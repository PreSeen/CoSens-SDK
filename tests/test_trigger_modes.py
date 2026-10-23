import json

from cosens_rsi.service import RSIService


def test_auto_approval_manual_trigger_decisions(tmp_path):
    service = RSIService(db_path=str(tmp_path / "rsi.sqlite"), memory_token="unused")
    task = {"objective": "请按步骤检查本项目的部署流程", "task_family": "ops"}

    service.runtime_mode = "auto"
    assert service.decide_trigger(task)["decision"] == "execute"
    assert service.decide_trigger({"objective": "What is 2+2?"})["decision"] == "do_not_trigger"

    service.runtime_mode = "approval"
    pending = service.request_approval(task, "test-principal")
    assert pending["decision"] == "approval_required"
    assert pending["scope_hash"]
    with service._db() as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        row = db.execute("SELECT status,task_json FROM approvals WHERE id=?", (pending["approval_id"],)).fetchone()
        assert row["status"] == "pending"
        assert json.loads(row["task_json"]) == task

    service.runtime_mode = "manual"
    assert service.decide_trigger(task)["decision"] == "do_not_trigger"
    assert service.decide_trigger(task, explicit=True)["decision"] == "execute"


def test_prepare_idempotency_reuses_and_rejects_changed_payload(tmp_path):
    service = RSIService(db_path=str(tmp_path / "rsi.sqlite"), memory_token="unused")
    service.runtime_mode = "auto"
    task = {"objective": "ordinary one-off question", "task_family": "general"}
    first = service.prepare_task(task, "test-principal", explicit=False, request_key="same-key")
    again = service.prepare_task(task, "test-principal", explicit=False, request_key="same-key")
    assert first == again
    try:
        service.prepare_task({"objective": "different", "task_family": "general"},
            "test-principal", explicit=False, request_key="same-key")
    except ValueError as exc:
        assert "idempotency" in str(exc)
    else:
        raise AssertionError("changed payload must conflict")

