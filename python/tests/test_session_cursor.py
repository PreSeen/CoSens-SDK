import pytest

from cosens.errors import RSIRecoveryRequired, SessionHistoryChanged
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
async def test_ack_rejects_unsent_or_duplicate_ids_without_advancing(tmp_path):
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(Session([{"id": "a", "role": "user", "content": "a"}]))
    store.begin_batch("run-1", snapshot, store.pending(snapshot))
    with pytest.raises(RSIRecoveryRequired):
        store.ack(snapshot, {"durable_ack": ["chat:a", "chat:future"]})
    with pytest.raises(RSIRecoveryRequired):
        store.ack(snapshot, {"durable_ack": ["chat:a", "chat:a"]})
    assert [event["id"] for event in store.recover_batch(snapshot).events] == ["chat:a"]
    assert store.ack(snapshot, {"durable_ack": ["chat:a"]}) == 1
    store.close()


@pytest.mark.asyncio
async def test_duplicate_session_message_ids_are_rejected_before_sending(tmp_path):
    session = Session([
        {"id": "same", "role": "user", "content": "first"},
        {"id": "same", "role": "assistant", "content": "second"},
    ])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    with pytest.raises(SessionHistoryChanged):
        await store.snapshot(session)
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


@pytest.mark.asyncio
async def test_stale_batch_cannot_ack_next_message(tmp_path):
    session = Session([{"id": "a", "role": "user", "content": "A"}, {"id": "b", "role": "user", "content": "B"}])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(session)
    stale = store.pending(snapshot, max_events=1)
    store.begin_batch("run-1", snapshot, stale)
    assert store.ack(snapshot, {"durable_ack": ["chat:a"]}) == 1
    with pytest.raises(RSIRecoveryRequired):
        store.begin_batch("run-2", snapshot, stale)
    assert [event["id"] for event in store.pending(snapshot)] == ["chat:b"]
    store.close()


@pytest.mark.asyncio
async def test_mutation_before_ack_does_not_redefine_acknowledged_payload(tmp_path):
    session = Session([{"id": "a", "role": "user", "content": "original"}])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(session)
    events = store.pending(snapshot)
    store.begin_batch("run-1", snapshot, events)
    session.items[0]["content"] = "rewritten"
    assert store.ack(snapshot, {"durable_ack": ["chat:a"]}) == 1
    with pytest.raises(SessionHistoryChanged):
        store.pending(await store.snapshot(session))
    store.close()


@pytest.mark.asyncio
async def test_recovered_batch_checks_current_history(tmp_path):
    session = Session([{"id": "a", "role": "user", "content": "original"}])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    snapshot = await store.snapshot(session)
    store.begin_batch("run-1", snapshot, store.pending(snapshot))
    session.items[0]["content"] = "rewritten"
    with pytest.raises(SessionHistoryChanged):
        store.recover_batch(await store.snapshot(session))
    store.close()


@pytest.mark.asyncio
async def test_two_cursor_instances_cannot_replace_another_runs_pending_batch(tmp_path):
    path = tmp_path / "state.sqlite3"
    session = Session([{"id": "a", "role": "user", "content": "A"}])
    first = CursorStore(path, principal="p")
    second = CursorStore(path, principal="p")
    first_snapshot = await first.snapshot(session)
    second_snapshot = await second.snapshot(session)
    first.begin_batch("run-1", first_snapshot, first.pending(first_snapshot))
    with pytest.raises(RSIRecoveryRequired):
        second.begin_batch("run-2", second_snapshot, second.pending(second_snapshot))
    assert first.recover_batch(first_snapshot).run_id == "run-1"
    first.close()
    second.close()
