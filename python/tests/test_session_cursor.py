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
async def test_duplicate_session_message_ids_are_rejected_before_sending(tmp_path):
    session = Session([
        {"id": "same", "role": "user", "content": "first"},
        {"id": "same", "role": "assistant", "content": "second"},
    ])
    store = CursorStore(tmp_path / "state.sqlite3", principal="p")
    with pytest.raises(SessionHistoryChanged):
        await store.snapshot(session)
    store.close()


def test_turn_state_survives_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = TurnStore(path)
    store.put("turn-1", {"session_id": "run-1", "state": "completion_pending", "result": {"summary": "done"}})
    store.close()
    reopened = TurnStore(path)
    assert reopened.get("turn-1")["result"]["summary"] == "done"
    reopened.close()
