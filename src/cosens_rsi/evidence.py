"""Live source validation shared by frozen Run and experiment read views."""

import json
from copy import deepcopy

from .algorithms.hierarchical import fingerprint


def source_refs(value):
    refs = []

    def walk(item):
        if isinstance(item, dict):
            if {"source_id", "revision", "content_hash", "start", "end"} <= item.keys():
                refs.append(
                    {
                        k: item[k]
                        for k in (
                            "source_id",
                            "revision",
                            "content_hash",
                            "start",
                            "end",
                        )
                    }
                )
            else:
                for v in item.values():
                    walk(v)
        elif isinstance(item, list):
            for v in item:
                walk(v)

    walk(value)
    return list({fingerprint(r): r for r in refs}.values())


def validate_snapshot(service, snapshot):
    refs = source_refs(snapshot)
    # The current Memory adapter accepts at most twelve refs. Batch explicitly
    # and check every ACK; its internal slice must never hide unvalidated refs.
    for start in range(0, len(refs), 12):
        batch = refs[start : start + 12]
        response = service._memory(
            "POST", "/internal/v1/evidence/expand", {"refs": batch}
        )
        if (
            response.get("validated_live") is not True
            or [item.get("ref") for item in response.get("items", [])] != batch
        ):
            raise LookupError("source references could not be live-validated")
    return {"validated_refs": len(refs)}


def training_trajectory(package, *, max_bytes=64_000):
    """Bound the proposer view without changing the lossless source capture.

    Historical MCP results can embed unpartitioned Memory answers. Retain their
    calls and delivery hashes; a trusted experiment read view supplies approved
    background. Full records and chunks remain privately addressable.
    """
    events, refs, used, excluded = [], [], 0, 0
    for event in package["events"]:
        refs.append(
            {"id": event["id"], "sha256": fingerprint(event), "kind": event["kind"]}
        )
        view = deepcopy(event)
        payload = view["payload"]
        native = payload.get("native", {})
        item = native.get("item", {}) if isinstance(native, dict) else {}
        if (
            isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and "result" in item
        ):
            item["result_sha256"] = fingerprint(item.pop("result"))
            item["result_excluded"] = "requires_experiment_memory_read_view"
            excluded += 1
        if event["kind"] == "context_delivery":
            view["payload"] = {
                "delivery_sha256": fingerprint(payload),
                "contents_excluded": "requires_experiment_memory_read_view",
            }
            excluded += 1
        size = len(json.dumps(view, ensure_ascii=False).encode())
        if used + size <= max_bytes:
            events.append(view)
            used += size
    return {
        "events": events,
        "source_refs": refs,
        "omitted_for_budget": len(refs) - len(events),
        "unpartitioned_contexts_excluded": excluded,
        "complete_action_trace": False,
    }
