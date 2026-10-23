"""Incremental native JSONL capture, separate from the lossy UI projection.

Advance byte cursors only after durable ACK. Replayed lines have stable event
IDs; UTF-8 partial records wait for their terminating newline. No event stream
is promoted to complete action telemetry merely because capture succeeded.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from .algorithms.hierarchical import fingerprint

PRIVATE_KEYS = re.compile(
    r"^(?:authorization|password|secret|api_key|access_token|admin_token|attempt_token|memory_token|token)$",
    re.IGNORECASE,
)


def _redact(value):
    from .codex_trace import PRIVATE_COMMAND, scrub

    if isinstance(value, dict):
        if value.get("type") == "command_execution" and PRIVATE_COMMAND.search(
            str(value.get("command", ""))
        ):
            return {
                "id": value.get("id"),
                "type": value["type"],
                "redacted_private_command": True,
                "exit_code": value.get("exit_code"),
                "coverage_gap": "credential_or_configuration_command",
            }
        return {
            k: "[redacted]" if PRIVATE_KEYS.fullmatch(k) else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return scrub(value) if isinstance(value, str) else value


def _bounded_payload(service, principal, event_id, payload):
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(text.encode()) <= 750_000:
        return payload
    refs = []
    # Content-addressed private chunks make a crash/retry reuse the same refs;
    # random artifact IDs here would change an otherwise idempotent event.
    with service._db() as db:
        from .service import now, sha

        for index in range(0, len(text), 180_000):
            chunk = text[index : index + 180_000]
            aid = "capture_" + fingerprint([principal, event_id, index, chunk])
            content_hash = sha(chunk)
            name = (
                "native-event-"
                + event_id.split(":")[-1]
                + "-"
                + str(index)
                + ".json.part"
            )
            db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)",
                (aid, principal, name, chunk, content_hash, now()),
            )
            refs.append(
                {"artifact_id": aid, "content_hash": content_hash, "index": index}
            )
    return {
        "schema": "chunked-native-event-v1",
        "encoding": "utf-8-json",
        "chunks": refs,
        "payload_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "size_bytes": len(text.encode()),
    }


def import_session_trace(service, path, principal="local-codex", *, run_id=None):
    from .codex_trace import tool_result

    source = Path(path).resolve()
    if run_id is not None:
        service._authorize_evidence_write(run_id, principal)
    stat = source.stat()
    source_key = fingerprint(str(source))
    with source.open("rb") as stream:
        first = stream.readline()
        if not first.endswith(b"\n"):
            return {
                "events_captured": 0,
                "byte_cursor": 0,
                "coverage": "partial_native_record",
            }
        identity = fingerprint([stat.st_dev, stat.st_ino, first.hex()])
        with service._db() as db:
            prior = db.execute(
                "SELECT * FROM native_capture_cursors WHERE principal=? AND source_key=?",
                (principal, source_key),
            ).fetchone()
        valid_prior = (
            prior
            and prior["identity"] == identity
            and prior["byte_cursor"] <= stat.st_size
        )
        cursor = prior["byte_cursor"] if valid_prior else 0
        thread = prior["thread_id"] if valid_prior else source.name
        current = run_id or (prior["run_id"] if valid_prior else None)
        stream.seek(cursor)
        records, pending = defaultdict(list), []
        last = cursor
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                break
            try:
                obj = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError):
                # Preserve a malformed complete record as opaque evidence,
                # rather than skipping it and claiming complete capture.
                obj = {
                    "type": "unparsed_native_record",
                    "raw_hex": line.hex(),
                    "coverage_gap": "invalid_native_json",
                }
            if not isinstance(obj, dict):
                obj = {"type": "unparsed_native_record", "value": obj}
            if obj.get("type") == "thread.started":
                thread = str(obj.get("thread_id", thread))
            item = obj.get("item") if isinstance(obj.get("item"), dict) else {}
            if (
                obj.get("type") == "item.completed"
                and item.get("type") == "mcp_tool_call"
            ):
                try:
                    result = tool_result(item)
                except (TypeError, AttributeError):
                    result = {}
                if (
                    item.get("server") == "cosens_rsi"
                    and item.get("tool") == "cosens_prepare_task"
                    and result.get("run_id")
                ):
                    bound = result["run_id"]
                    service._authorize_evidence_write(bound, principal)
                    current = bound
            kind = "agent_action"
            if (
                item.get("type") in {"command_execution", "mcp_tool_call"}
                and obj.get("type") == "item.completed"
            ):
                kind = "tool_result"
            elif item.get("type") == "file_change":
                kind = "artifact"
            elif obj.get("type") in {"turn.completed", "turn.failed"}:
                kind = "session_end"
            # Reasoning summaries are not the observable tool trajectory.
            if item.get("type") == "reasoning":
                obj = {
                    "type": obj.get("type"),
                    "item": {"id": item.get("id"), "type": "reasoning_omitted"},
                }
            eid = "native:" + fingerprint([identity, thread, offset])
            payload = _bounded_payload(
                service,
                principal,
                eid,
                {
                    "native": _redact(obj),
                    "source": source.name,
                    "byte_start": offset,
                    "byte_end": stream.tell(),
                    "coverage": "native_observable_events_not_complete_action_trace",
                },
            )
            event = {"id": eid, "kind": kind, "payload": payload}
            if current:
                if pending:
                    records[current].extend(pending)
                    pending = []
                records[current].append(event)
            else:
                pending.append(event)
            last = stream.tell()
    if pending:
        # There is no explicit authorized run binding yet. Leave the cursor
        # unchanged; the original file remains available for a later import.
        return {
            "events_captured": 0,
            "byte_cursor": cursor,
            "coverage": "awaiting_run_binding",
        }
    count = 0
    for rid, events in records.items():
        batch, size = [], 0
        for event in events:
            event_size = len(json.dumps(event, ensure_ascii=False).encode())
            if batch and (len(batch) >= 100 or size + event_size > 900_000):
                service.capture_session_events(rid, principal, batch)
                count += len(batch)
                batch, size = [], 0
            batch.append(event)
            size += event_size
        if batch:
            service.capture_session_events(rid, principal, batch)
            count += len(batch)
    with service._db() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = db.execute(
            "SELECT identity,byte_cursor FROM native_capture_cursors WHERE principal=? AND source_key=?",
            (principal, source_key),
        ).fetchone()
        # Compare-and-swap prevents a slow importer from moving the cursor
        # behind another importer's durable ACK. Duplicate event writes are safe.
        if latest is None or (
            prior and tuple(latest) == (prior["identity"], prior["byte_cursor"])
        ):
            db.execute(
                "INSERT INTO native_capture_cursors VALUES(?,?,?,?,?,?) ON CONFLICT(principal,source_key) "
                "DO UPDATE SET identity=excluded.identity,byte_cursor=excluded.byte_cursor,"
                "thread_id=excluded.thread_id,run_id=excluded.run_id",
                (principal, source_key, identity, last, thread, current),
            )
    return {
        "events_captured": count,
        "byte_cursor": last,
        "coverage": "native_observable_events",
    }
