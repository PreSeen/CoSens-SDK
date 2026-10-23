"""Durable native capture fault injection, independent of Memory availability."""

import json

import pytest
from test_generic_improvement import service, task

from cosens_rsi.capture import import_session_trace
from cosens_rsi.codex_trace import import_trace


def line(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode()


def captured(rsi, run):
    with rsi._db() as db:
        return [
            json.loads(r[0])
            for r in db.execute(
                "SELECT body_json FROM canonical_session_events "
                "WHERE run_id=? AND event_id LIKE 'native:%' ORDER BY received_at",
                (run,),
            )
        ]


def test_byte_cursor_waits_for_utf8_record_and_duplicate_import_does_not_wake(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    path = tmp_path / "native.jsonl"
    first = line({"type": "thread.started", "thread_id": "thread-1"})
    second = line(
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "read input",
                "aggregated_output": "完整中文结果",
            },
        }
    )
    split = second.index("中".encode()) + 1
    path.write_bytes(first + second[:split])
    initial = import_session_trace(rsi, path, "owner", run_id=run)
    assert initial["byte_cursor"] == len(first)
    with path.open("ab") as output:
        output.write(second[split:])
    final = import_session_trace(rsi, path, "owner", run_id=run)
    assert final["byte_cursor"] == len(first + second)
    assert (
        captured(rsi, run)[-1]["payload"]["native"]["item"]["aggregated_output"]
        == "完整中文结果"
    )
    with rsi._db() as db:
        generation = db.execute(
            "SELECT generation FROM generic_evidence_generations WHERE principal='owner'"
        ).fetchone()[0]
    assert import_session_trace(rsi, path, "owner", run_id=run)["events_captured"] == 0
    with rsi._db() as db:
        assert (
            db.execute(
                "SELECT generation FROM generic_evidence_generations WHERE principal='owner'"
            ).fetchone()[0]
            == generation
        )


def test_ack_failure_does_not_advance_cursor_and_retry_does_not_duplicate(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    path = tmp_path / "native.jsonl"
    path.write_bytes(
        line({"type": "thread.started", "thread_id": "thread-1"})
        + b"".join(
            line(
                {
                    "type": "item.completed",
                    "item": {"id": str(i), "type": "agent_message", "text": str(i)},
                }
            )
            for i in range(120)
        )
    )
    original = rsi.capture_session_events

    def lost_ack(*args):
        original(*args)
        raise ConnectionError("ack lost after commit")

    monkeypatch.setattr(rsi, "capture_session_events", lost_ack)
    with pytest.raises(ConnectionError):
        import_session_trace(rsi, path, "owner", run_id=run)
    with rsi._db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM native_capture_cursors").fetchone()[0] == 0
        )
    monkeypatch.setattr(rsi, "capture_session_events", original)
    import_session_trace(rsi, path, "owner", run_id=run)
    assert len(captured(rsi, run)) == 121


def test_large_native_output_is_lossless_private_chunks_with_stable_refs(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    path = tmp_path / "native.jsonl"
    text = "完整输出\n" * 100_000
    path.write_bytes(
        line(
            {
                "type": "item.completed",
                "item": {
                    "id": "huge",
                    "type": "command_execution",
                    "command": "read public data",
                    "aggregated_output": text,
                },
            }
        )
    )
    import_session_trace(rsi, path, "owner", run_id=run)
    event = captured(rsi, run)[0]
    payload = event["payload"]
    assert payload["schema"] == "chunked-native-event-v1"
    with rsi._db() as db:
        chunks = [
            db.execute(
                "SELECT content FROM artifacts WHERE id=? AND principal='owner'",
                (r["artifact_id"],),
            ).fetchone()[0]
            for r in payload["chunks"]
        ]
        count = db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        db.execute("DELETE FROM native_capture_cursors")
    assert json.loads("".join(chunks))["native"]["item"]["aggregated_output"] == text
    import_session_trace(rsi, path, "owner", run_id=run)
    with rsi._db() as db:
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == count
    with pytest.raises(LookupError):
        import_session_trace(rsi, path, "other", run_id=run)


def test_learning_capture_keeps_other_tools_and_full_context_but_redacts_secrets(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    path = tmp_path / "native.jsonl"
    native = {
        "type": "item.completed",
        "item": {
            "id": "external",
            "type": "mcp_tool_call",
            "server": "external-search",
            "tool": "query",
            "arguments": {"q": "original prompt", "api_key": "sensitive"},
            "result": {"content": [{"type": "text", "text": "background" * 1000}]},
        },
    }
    path.write_bytes(line(native))
    # Capturing existing evidence is independent from online Memory reads.
    rsi._memory = lambda *args: (_ for _ in ()).throw(ConnectionError("Memory offline"))
    imported = import_trace(
        rsi, path, "owner", run_id=run, user_prompt="The complete original user prompt"
    )
    assert imported["items_imported"] == 0  # Other tools stay out of the UI projection.
    event = captured(rsi, run)[0]
    assert event["payload"]["native"]["item"]["arguments"]["api_key"] == "[redacted]"
    assert (
        len(event["payload"]["native"]["item"]["result"]["content"][0]["text"])
        == 10_000
    )
    with rsi._db() as db:
        prompt = db.execute(
            "SELECT body_json FROM canonical_session_events WHERE event_id LIKE 'native-prompt:%'"
        ).fetchone()[0]
    assert "The complete original user prompt" in prompt


def test_file_replacement_has_a_new_capture_identity(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    path = tmp_path / "native.jsonl"
    for thread in ("old", "new"):
        path.write_bytes(line({"type": "thread.started", "thread_id": thread}))
        import_session_trace(rsi, path, "owner", run_id=run)
    assert len(captured(rsi, run)) == 2


def test_proposer_view_excludes_unpartitioned_memory_and_reports_budget_gaps():
    from cosens_rsi.evidence import training_trajectory

    raw = {
        "id": "recall",
        "kind": "tool_result",
        "payload": {
            "native": {
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "memory_recall",
                    "result": {"background": "UNEXPOSED_CONFIRM_ANSWER"},
                }
            }
        },
    }
    package = {"events": [raw]}
    view = training_trajectory(package)
    assert "UNEXPOSED_CONFIRM_ANSWER" not in json.dumps(view)
    assert view["unpartitioned_contexts_excluded"] == 1
    assert (
        package["events"][0]["payload"]["native"]["item"]["result"]["background"]
        == "UNEXPOSED_CONFIRM_ANSWER"
    )
    limited = training_trajectory(package, max_bytes=0)
    assert limited["events"] == [] and limited["omitted_for_budget"] == 1
    assert limited["source_refs"][0]["id"] == "recall"
