"""Durable per-Session cursor for incremental, ACK-driven event reporting."""

import hashlib
import inspect
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from ._config import SERVICE_ID
from .errors import RSIRecoveryRequired, SessionHistoryChanged


def default_state_path():
    return Path.home() / ".cosens" / "state.sqlite3"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _plain(item):
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    if hasattr(item, "__dict__"):
        return vars(item)
    raise TypeError("Session items must be dictionaries or serializable objects")


@dataclass(frozen=True)
class SessionSnapshot:
    key: str
    session_id: str
    items: tuple[dict, ...]


@dataclass(frozen=True)
class PendingBatch:
    run_id: str
    events: list[dict]


class CursorStore:
    def __init__(self, path=None, *, principal):
        path = str(path or default_state_path())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, acked_index INTEGER NOT NULL, "
            "prefix_hash TEXT, run_id TEXT, pending_events TEXT)"
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(cursors)")}
        for column in ("acked_message_id", "acked_item_hash", "source_revision"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE cursors ADD COLUMN {column} TEXT")
        self._db.commit()
        self._principal = _hash(principal)
        self._lock = threading.RLock()

    async def snapshot(self, session):
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
        if not session_id:
            raise ValueError("Session must expose a stable session_id or id")
        incarnation = getattr(session, "session_incarnation", None) or getattr(session, "incarnation", "0")
        items = session.get_items()
        if inspect.isawaitable(items):
            items = await items
        if not isinstance(items, (list, tuple)):
            raise TypeError("Session.get_items() must return a list")
        key = _hash([SERVICE_ID, self._principal, str(session_id), str(incarnation)])
        return SessionSnapshot(key, str(session_id), tuple(_plain(item) for item in items))

    def _row(self, key):
        row = self._db.execute(
            "SELECT acked_index, prefix_hash, run_id, pending_events FROM cursors WHERE key=?", (key,)
        ).fetchone()
        return row or (-1, None, None, None)

    def _validate(self, snapshot, index, prefix_hash):
        if index >= len(snapshot.items) or (index >= 0 and _hash(snapshot.items[: index + 1]) != prefix_hash):
            raise SessionHistoryChanged("Previously ACKed Session history changed; use a new session incarnation or reconcile")

    def pending(self, snapshot, *, max_events=200, max_bytes=1_000_000):
        with self._lock:
            index, prefix_hash, _, _ = self._row(snapshot.key)
            self._validate(snapshot, index, prefix_hash)
            events = []
            size = 0
            for position in range(index + 1, len(snapshot.items)):
                item = snapshot.items[position]
                role = str(item.get("role", item.get("type", "unknown")))
                message_id = str(item.get("id") or _hash([snapshot.session_id, position, item]))
                kind = {"user": "user_prompt", "assistant": "agent_action", "tool": "tool_result"}.get(role, "context_delivery")
                event = {
                    "id": f"chat:{message_id}",
                    "kind": kind,
                    "payload": {
                        "session_id": snapshot.session_id,
                        "message_id": message_id,
                        "index": position,
                        "role": role,
                        "content": item.get("content", item),
                        "source_revision": item.get("source_revision"),
                    },
                }
                encoded_size = len(_json(event).encode("utf-8"))
                if encoded_size > max_bytes:
                    raise ValueError("One Session event exceeds the server batch size")
                if events and (len(events) >= max_events or size + encoded_size > max_bytes):
                    break
                events.append(event)
                size += encoded_size
            return events

    def begin_batch(self, run_id, snapshot, events):
        if not events:
            return
        with self._lock, self._db:
            acked_index, prefix_hash, old_run_id, pending_json = self._row(snapshot.key)
            self._validate(snapshot, acked_index, prefix_hash)
            if pending_json and old_run_id != run_id:
                raise RSIRecoveryRequired(f"Pending Session events belong to Run {old_run_id}")
            self._db.execute(
                "INSERT INTO cursors(key,acked_index,prefix_hash,run_id,pending_events) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id,pending_events=excluded.pending_events",
                (snapshot.key, acked_index, prefix_hash, run_id, _json(events)),
            )

    def recover_batch(self, snapshot):
        with self._lock:
            _, _, run_id, pending_json = self._row(snapshot.key)
            if not pending_json:
                return None
            return PendingBatch(run_id, json.loads(pending_json))

    def ack(self, snapshot, receipt):
        acknowledged = receipt.get("durable_ack")
        if isinstance(acknowledged, dict):
            acknowledged = acknowledged.get("event_ids", acknowledged.get("ids", []))
        if not isinstance(acknowledged, list):
            raise RSIRecoveryRequired("Session event receipt has no durable_ack event IDs")
        ack_set = set(acknowledged)
        with self._lock, self._db:
            old_index, old_hash, run_id, pending_json = self._row(snapshot.key)
            self._validate(snapshot, old_index, old_hash)
            if not pending_json:
                raise RSIRecoveryRequired("No pending Session event batch to acknowledge")
            pending = json.loads(pending_json)
            advanced = 0
            for event in pending:
                if event["id"] not in ack_set:
                    break
                advanced += 1
            new_index = old_index + advanced
            remaining = pending[advanced:]
            item = snapshot.items[new_index] if new_index >= 0 else {}
            updated = self._db.execute(
                "UPDATE cursors SET acked_index=?,prefix_hash=?,pending_events=?,"
                "acked_message_id=?,acked_item_hash=?,source_revision=? "
                "WHERE key=? AND acked_index=? AND run_id=?",
                (
                    new_index,
                    _hash(snapshot.items[: new_index + 1]) if new_index >= 0 else None,
                    _json(remaining) if remaining else None,
                    str(item.get("id") or _hash([snapshot.session_id, new_index, item])) if new_index >= 0 else None,
                    _hash(item) if new_index >= 0 else None,
                    item.get("source_revision"),
                    snapshot.key,
                    old_index,
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise RSIRecoveryRequired("Session cursor changed concurrently; reread before ACK")
            return advanced

    def last_ack(self, snapshot):
        with self._lock:
            row = self._db.execute(
                "SELECT acked_index,acked_message_id,acked_item_hash,source_revision,run_id "
                "FROM cursors WHERE key=?", (snapshot.key,),
            ).fetchone()
            if row is None:
                return None
            return dict(zip(("index", "message_id", "item_hash", "source_revision", "run_id"), row))

    def close(self):
        self._db.close()
