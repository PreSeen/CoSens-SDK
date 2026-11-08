import pytest

from cosens.local import LocalRSI
from local_rsi_helpers import FakeModel, complete_execution, first_revision, make_rsi


async def test_create_task_replays_and_rejects_changed_request(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    first = await rsi.create_task(source="new_task", key="k", objective="A")
    assert first.status == "ready" and first.context_version == 1
    assert (await rsi.create_task(source="new_task", key="k", objective="A")).task_id == first.task_id
    with pytest.raises(ValueError, match="idempotency"):
        await rsi.create_task(source="new_task", key="k", objective="B")


async def test_task_without_objective_needs_context_until_supplemented(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(source="new_task", key="k")
    assert task.status == "needs_context" and task.missing == ["objective"]
    started = await rsi.start_execution(task.task_id, key="e")
    assert started.status == "needs_context"
    updated = await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="A")
    assert updated.status == "ready" and updated.context_version == 2
    with pytest.raises(ValueError, match="version changed"):
        await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="B")


async def test_artifact_round_trip_and_tamper_detection(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    saved = await rsi.artifact(name="a.txt", content="hello")
    assert (await rsi.get_artifact(saved.artifact_id)).content == "hello"
    with rsi._store.transaction() as db:
        db.execute("UPDATE artifacts SET content='changed'")
    with pytest.raises(ValueError, match="changed"):
        await rsi.get_artifact(saved.artifact_id)


async def test_execution_freezes_request_and_replays(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e")
    assert started.status == "awaiting_execution" and started.request["context"]["objective"] == "A"
    assert "method" not in started.request and "memory" not in started.request
    assert (await rsi.start_execution(task.task_id, key="e")).execution_id == started.execution_id
    assert (await rsi.get_execution(started.execution_id)).request_sha256 == started.request_sha256


async def test_submit_requires_matching_hash_and_freezes_result(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e")
    artifact = await rsi.artifact(name="o", content="x")
    ref = [{"artifact_id": artifact.artifact_id, "role": "submitted_output"}]
    with pytest.raises(ValueError, match="hash mismatch"):
        await rsi.submit_execution(started.execution_id, request_sha256="wrong", outcome="completed", artifacts=ref)
    done = await rsi.submit_execution(started.execution_id, request_sha256=started.request_sha256, outcome="completed", artifacts=ref)
    assert done.revision_id
    replay = await rsi.submit_execution(started.execution_id, request_sha256=started.request_sha256, outcome="completed", artifacts=ref)
    assert replay.revision_id == done.revision_id
    with pytest.raises(ValueError, match="frozen"):
        await rsi.submit_execution(started.execution_id, request_sha256=started.request_sha256, outcome="failed", artifacts=[])


async def test_failed_execution_creates_no_revision(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e")
    done = await rsi.submit_execution(started.execution_id, request_sha256=started.request_sha256, outcome="failed", artifacts=[])
    assert done.status == "failed" and done.revision_id is None


async def test_feedback_requires_source_and_replays(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    _, _, done = await first_revision(rsi)
    with pytest.raises(ValueError, match="source"):
        await rsi.record_feedback(done.revision_id, feedback="x", source="guess", key="f")
    saved = await rsi.record_feedback(done.revision_id, feedback="x", source="human", key="f")
    assert saved.message == "x" and saved.source == "human"
    assert (await rsi.record_feedback(done.revision_id, feedback="x", source="human", key="f")).feedback_id == saved.feedback_id
    with pytest.raises(ValueError, match="different feedback"):
        await rsi.record_feedback(done.revision_id, feedback="y", source="human", key="f")


async def test_revision_carries_base_result_and_feedback(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    _, _, done = await first_revision(rsi)
    await rsi.record_feedback(done.revision_id, feedback="fix it", source="evaluator", key="f")
    started = await rsi.start_revision(done.revision_id, key="r")
    assert started.request["base_revision_id"] == done.revision_id
    assert started.request["feedback"][0]["message"] == "fix it"
    assert started.request["base_result"]["events"][0]["id"] == "e1"
    again = await complete_execution(rsi, started, key="second")
    assert (await rsi.get_revision(again.revision_id)).trace_origin == "revision"
    assert (await rsi.start_revision(done.revision_id, key="r")).execution_id == started.execution_id


async def test_list_tasks_paginates_newest_first(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    ids = [(await rsi.create_task(source="new_task", key=str(i), objective="A")).task_id for i in range(3)]
    page = await rsi.list_tasks(limit=2)
    assert [item.task_id for item in page["items"]] == [ids[2], ids[1]] and page.has_more
    rest = await rsi.list_tasks(limit=2, cursor=page.next_cursor)
    assert [item.task_id for item in rest["items"]] == [ids[0]] and not rest.has_more


async def test_recall_memory_requires_a_memory_client(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    with pytest.raises(ValueError, match="memory client"):
        await rsi.start_execution(task.task_id, key="e", recall_memory=True)


async def test_context_changed_during_recall_is_refused(tmp_path):
    rsi = LocalRSI(memory=None, model=FakeModel(), state_path=tmp_path / "s.sqlite3")
    task = await rsi.create_task(source="new_task", key="k", objective="Old")

    class Atoms:
        async def memory_recall(self, *, query, token_budget):
            await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="New")
            return {"items": []}

    rsi._atoms = Atoms()
    with pytest.raises(ValueError, match="changed while recalling"):
        await rsi.start_execution(task.task_id, key="e", recall_memory=True)


async def test_unknown_ids_raise_lookup_error(tmp_path):
    rsi = LocalRSI(memory=None, model=FakeModel(), state_path=tmp_path / "s.sqlite3")
    for call in (rsi.get_task("x"), rsi.get_execution("x"), rsi.get_revision("x"), rsi.get_artifact("x")):
        with pytest.raises(LookupError):
            await call
