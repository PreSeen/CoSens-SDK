import pytest

from cosens.rsi import Conflict, NotFound, RSIClient


def test_task_tracks_explicit_successors_and_keeps_restart_evidence(tmp_path):
    source = tmp_path / "context.txt"
    source.write_text("initial context")
    evidence = {"context_path": str(source), "files": []}
    data_dir = tmp_path / "data"
    client = RSIClient(
        data_dir=data_dir, model_id="fixture", api_key="fixture",
        base_url="https://model.example/v1", config={"auto_start": False},
    )
    root = client.execution_create(evidence=evidence, idempotency_key="first")
    task_id = root["id"]
    store = client._queue.store
    question = {"id": "question_1", "revision": 1, "question": "Which sample?", "options": []}
    with store.transaction() as conn:
        row = store.get_in(conn, task_id, "execution")
        store.save(conn, {**row, "questions": [question]})
    pending = client.task_get(task_id)
    assert pending["pending_question_count"] == 1
    assert pending["pending_questions"][0]["execution_id"] == task_id
    assert pending["clarification_history"][0]["answer"] is None

    clarified = client.execution_clarification_answer(
        task_id, question_id=question["id"], question_revision=1, answer="The first sample."
    )["continuation_execution_id"]
    ordinary_child = client._queue.execution_create(
        material_ids=store.get(task_id, "execution")["material_ids"],
        policy=store.get(task_id, "execution")["policy"], base_execution_id=task_id,
    )["id"]
    with pytest.raises(NotFound):
        client.task_get(clarified)
    assert {entry["task_id"] for entry in client.task_list()} == {task_id, ordinary_child}

    with store.transaction() as conn:
        row = store.get_in(conn, clarified, "execution")
        next_question = {**question, "id": "question_2", "revision": row["input_revision"]}
        store.save(conn, {**row, "questions": [next_question]})

    new_source = tmp_path / "new-context.txt"
    new_source.write_text("additional context")
    new_evidence = {"context_path": str(new_source), "files": []}
    restarted = client.task_restart(task_id, idempotency_key="restart-key", evidence=new_evidence)
    successor = restarted["execution_id"]
    assert successor not in {task_id, clarified}
    assert restarted["task_id"] == task_id
    assert store.get(clarified, "execution")["status"] == "cancelled"
    assert client.execution_get(successor)["base_execution_id"] == clarified
    assert len(client.execution_get(successor)["evidence"]) == 2
    assert client.execution_get(successor)["questions"] == []
    assert client.execution_get(successor)["answers"]["question_1:1"]["text"] == "The first sample."
    with pytest.raises(Conflict, match="superseded"):
        client.execution_clarification_answer(
            clarified, question_id=next_question["id"],
            question_revision=next_question["revision"], answer="Stale answer.",
        )

    reopened = RSIClient(data_dir=data_dir)
    assert reopened.task_restart(task_id, idempotency_key="restart-key", evidence=new_evidence)["execution_id"] == successor
    with pytest.raises(Conflict, match="different arguments"):
        reopened.task_restart(task_id, idempotency_key="restart-key")
    detail = reopened.task_get(task_id)
    assert detail["execution_id"] == successor
    assert detail["execution_ids"] == [task_id, clarified, successor]
    assert detail["clarification_history"][0]["answer"]["text"] == "The first sample."
    assert len(detail["clarification_history"]) == 2
    assert detail["clarification_history"][1]["answer"] is None
    assert detail["pending_questions"] == []
    assert detail["artifact"] is None


def test_appending_evidence_rejects_stale_clarification(tmp_path):
    source = tmp_path / "context.txt"
    source.write_text("initial context")
    client = RSIClient(
        data_dir=tmp_path / "data", model_id="fixture", api_key="fixture",
        base_url="https://model.example/v1", config={"auto_start": False},
    )
    execution = client.execution_create(
        evidence={"context_path": str(source), "files": []}, idempotency_key="first",
    )
    execution_id = execution["id"]
    store = client._queue.store
    with store.transaction() as conn:
        row = store.get_in(conn, execution_id, "execution")
        store.save(conn, {**row, "questions": [
            {"id": "question_1", "revision": 1, "question": "Which answer?", "options": []},
        ]})

    source.write_text("updated context")
    client.execution_append_evidence(execution_id, evidence={"context_path": str(source), "files": []})
    with pytest.raises(Conflict, match="question revision is stale"):
        client.execution_clarification_answer(
            execution_id, question_id="question_1", question_revision=1, answer="Outdated.",
        )
    detail = client.task_get(execution_id)
    assert detail["pending_questions"] == []
    assert detail["execution_ids"] == [execution_id]
    assert detail["status"] == "queued"
