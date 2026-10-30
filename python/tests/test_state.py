import sqlite3

import pytest

from cosens.state import TurnStore


def test_turn_and_recovery_aliases_commit_atomically(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = TurnStore(path, principal="key", service_id="service")
    db = sqlite3.connect(path)
    db.execute("CREATE TRIGGER fail_alias BEFORE INSERT ON turns WHEN NEW.turn_id LIKE '%logical:%' BEGIN SELECT RAISE(FAIL, 'alias write failed'); END")
    db.commit()
    state = {"turn_id": "turn-1", "state": "pre_execution_pending"}
    with pytest.raises(sqlite3.IntegrityError, match="alias write failed"):
        store.put_many([("turn-1", state), ("logical:one", state)])
    assert store.get("turn-1") is None
    assert store.get("logical:one") is None
    db.execute("DROP TRIGGER fail_alias")
    db.commit()
    store.put_many([("turn-1", state), ("logical:one", state)])
    assert store.get("turn-1") == store.get("logical:one")
    db.close()
    store.close()


def test_workflow_native_dispatch_claim_is_atomic_across_store_instances(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = TurnStore(path, principal="key", service_id="service")
    second = TurnStore(path, principal="key", service_id="service")
    pending = {"turn_id": "turn-1", "state": "pre_execution_pending", "task_spec": {"source": "new_task"}, "mode": "auto"}
    assert first.insert_if_absent("turn-1", pending)
    assert not second.insert_if_absent("turn-1", pending)
    prepared = {**pending, "task_id": "t1", "execution_id": "e1", "request_sha256": "h1"}
    assert second.update_pending("turn-1", prepared)
    assert not first.update_pending("turn-1", pending)
    running = {**prepared, "state": "native_running"}
    assert first.claim_state("turn-1", expected_state="pre_execution_pending", value=running)
    assert not second.claim_state("turn-1", expected_state="pre_execution_pending", value=running)
    assert not second.update_pending("turn-1", prepared)
    assert first.get("turn-1")["state"] == "native_running"
    first.close()
    second.close()
