import pytest

from cosens.errors import SessionHistoryChanged
from cosens.session_cursor import CursorStore
from cosens.state import TurnStore


class Session:
    session_id = "chat-1"

    def __init__(self, items):
        self.items = items

    async def get_items(self):
        return self.items


@pytest.mark.asyncio
async def test_acknowledged_messages_are_not_resent_across_runs_or_restarts(tmp_path):
    path = tmp_path / "state.sqlite3"
    session = Session([{"id": "u1", "role": "user", "content": "hello", "source_revision": "rev-1"}])
    store = CursorStore(path, principal="principal-a")
    first = await store.snapshot(session)
    events = store.pending(first)
    assert len(events) == 1
    assert events[0]["id"] == "chat:u1"
    store.begin_batch("run-1", first, events)
    assert store.ack(first, {"durable_ack": ["chat:u1"]}) == 1
    assert store.last_ack(first)["message_id"] == "u1"
    assert store.last_ack(first)["source_revision"] == "rev-1"
    assert store.last_ack(first)["item_hash"]
    store.close()

    reopened = CursorStore(path, principal="principal-a")
    session.items.append({"id": "a1", "role": "assistant", "content": "world"})
    second = await reopened.snapshot(session)
    assert [e["id"] for e in reopened.pending(second)] == ["chat:a1"]
    reopened.close()


@pytest.mark.asyncio
async def test_partial_ack_keeps_unconfirmed_suffix_for_same_run(tmp_path):
    session = Session([
        {"id": "u1", "role": "user", "content": "one"},
        {"id": "u2", "role": "user", "content": "two"},
        {"id": "u3", "role": "user", "content": "three"},
    ])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(session)
    events = store.pending(snapshot)
    store.begin_batch("run-1", snapshot, events)
    assert store.ack(snapshot, {"durable_ack": ["chat:u1", "chat:u3"]}) == 1
    assert [e["id"] for e in store.pending(snapshot)] == ["chat:u2", "chat:u3"]
    assert store.recover_batch(snapshot).run_id == "run-1"
    store.close()


@pytest.mark.asyncio
async def test_rewriting_acknowledged_history_raises_instead_of_skipping(tmp_path):
    session = Session([{"id": "u1", "role": "user", "content": "original"}])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(session)
    events = store.pending(snapshot)
    store.begin_batch("run-1", snapshot, events)
    store.ack(snapshot, {"durable_ack": ["chat:u1"]})
    session.items[0]["content"] = "rewritten"
    with pytest.raises(SessionHistoryChanged):
        store.pending(await store.snapshot(session))
    store.close()


def test_turn_state_survives_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = TurnStore(path)
    store.put("turn-1", {"run_id": "run-1", "state": "completion_pending", "result": {"summary": "done"}})
    store.close()
    reopened = TurnStore(path)
    assert reopened.get("turn-1")["result"]["summary"] == "done"
    reopened.close()
