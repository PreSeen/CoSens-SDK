import pytest

from cosens.local import LocalRSI
from local_rsi_helpers import FakeModel, complete_execution, first_revision, make_rsi, recipe_reply


async def test_create_task_replays_and_rejects_changed_request(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    first = await rsi.create_task(key="k", objective="A")
    assert set(first) == {"task_id", "objective", "artifacts", "context_version"}
    assert (first.objective, first.artifacts, first.context_version) == ("A", [], 1)
    assert (await rsi.create_task(key="k", objective="A")).task_id == first.task_id
    with pytest.raises(ValueError, match="idempotency"):
        await rsi.create_task(key="k", objective="B")


async def test_a_task_holds_only_the_capability_and_its_reference_material(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    template = await rsi.artifact(name="template.md", content="## Risks")
    task = await rsi.create_task(key="k", objective="A", artifacts=[template.artifact_id])
    assert [(a["artifact_id"], a["role"]) for a in task.artifacts] == [(template.artifact_id, "reference")]
    view = await rsi.get_task(task.task_id)
    assert set(view) == {"task_id", "objective", "artifacts", "context_version", "executions", "recipes"}
    assert view.executions == [] and view.recipes == [] and view.artifacts == task.artifacts
    data = await rsi.artifact(name="in.txt", content="x")
    with pytest.raises(ValueError):
        await rsi.create_task(key="k2", objective="A", artifacts=[{"artifact_id": data.artifact_id, "role": "original_input"}])


async def test_context_updates_change_only_the_capability(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(key="k", objective="A")
    note = await rsi.artifact(name="terms.md", content="Revenue means booked revenue.")
    updated = await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="B",
                                                artifacts=[note.artifact_id])
    assert (updated.objective, updated.context_version) == ("B", 2)
    assert [(a["artifact_id"], a["role"]) for a in updated.artifacts] == [(note.artifact_id, "reference")]
    with pytest.raises(ValueError, match="version changed"):
        await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="C")
    with pytest.raises(ValueError, match="already attached"):
        await rsi.supplement_task_context(task.task_id, expected_context_version=2,
                                          artifacts=[{"artifact_id": note.artifact_id, "role": "reference"}])
    with pytest.raises(TypeError):
        await rsi.supplement_task_context(task.task_id, expected_context_version=2, self_contained=True)


async def test_an_incomplete_execution_input_is_refused_with_what_is_missing(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(key="k", objective="A")
    with pytest.raises(ValueError, match="^external_artifact requires submitted_output$"):
        await rsi.start_execution(task.task_id, key="e1", source="external_artifact", objective="A")
    produced = await rsi.artifact(name="report.md", content="Week 1 report")
    with pytest.raises(ValueError, match="^the execution input is incomplete: original_input_or_self_contained_task$"):
        await rsi.start_execution(task.task_id, key="e2", source="external_artifact", objective="A",
                                  artifacts=[produced.artifact_id])
    with pytest.raises(ValueError, match="^the execution input is incomplete: objective$"):
        await rsi.start_execution(task.task_id, key="e3", source="new_task")
    reconstructed = await rsi.start_execution(task.task_id, key="e4", source="external_artifact", objective="A",
                                              artifacts=[produced.artifact_id], self_contained=True)
    assert reconstructed.kind == reconstructed.request["kind"] == "reconstructed"
    assert [a["role"] for a in reconstructed.request["input"]["artifacts"]] == ["submitted_output"]


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
    task = await rsi.create_task(key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Week 1")
    assert started.status == "awaiting_execution" and started.request["input"]["objective"] == "Week 1"
    assert started.request["input"] == {"objective": "Week 1", "self_contained": False, "artifacts": []}
    assert started.request["task_context"] == {"objective": "A", "artifacts": [], "context_version": 1}
    assert started.instance_id == started.execution_id and "context" not in started.request
    assert started.kind == "original"
    assert "method" not in started.request and "memory" not in started.request
    again = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Week 1")
    assert again.execution_id == started.execution_id and again.kind == "original"
    with pytest.raises(ValueError, match="idempotency"):
        await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Week 2")
    # The key covers what the caller sent, so a supplement of the shared task does not strand a resent start.
    await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="A v2")
    resent = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Week 1")
    assert resent.execution_id == started.execution_id and resent.request["task_context"]["context_version"] == 1
    execution = await rsi.get_execution(started.execution_id)
    assert execution.request_sha256 == started.request_sha256 and execution.instance_id == started.execution_id
    [listed] = (await rsi.get_task(task.task_id)).executions
    assert listed == {"execution_id": started.execution_id, "instance_id": started.execution_id, "kind": "original",
                      "status": "awaiting_execution", "request_sha256": started.request_sha256}


async def test_submit_requires_matching_hash_and_freezes_result(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="A")
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
    task = await rsi.create_task(key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="A")
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
    # A correction keeps the context its first draft saw, not the task's current one.
    glossary = await rsi.artifact(name="glossary.md", content="ARR means annual recurring revenue.")
    await rsi.supplement_task_context(done.task_id, expected_context_version=1, artifacts=[glossary.artifact_id])
    started = await rsi.start_revision(done.revision_id, key="r")
    assert started.request["base_revision_id"] == done.revision_id
    first = await rsi.get_execution(done.execution_id)
    assert started.request["input"] == first.request["input"]
    assert started.request["task_context"] == first.request["task_context"]
    assert started.request["task_context"]["context_version"] == 1 and started.request["task_context"]["artifacts"] == []
    assert started.instance_id == done.execution_id and started.kind == "revision"
    assert (await rsi.get_execution(started.execution_id)).instance_id == done.execution_id
    assert [item["message"] for item in started.request["feedback"]] == ["Keep this method", "fix it"]
    assert started.request["base_result"]["events"][0]["id"] == "e1"
    again = await complete_execution(rsi, started, key="second")
    assert (await rsi.get_revision(again.revision_id)).trace_origin == "revision"
    assert (await rsi.start_revision(done.revision_id, key="r")).execution_id == started.execution_id
    # A correction of a correction stays in the instance of the first execution.
    await rsi.record_feedback(again.revision_id, feedback="still wrong", source="human", key="f2")
    later = await rsi.start_revision(again.revision_id, key="r2")
    assert later.request["base_revision_id"] == again.revision_id
    assert later.instance_id == done.execution_id
    assert (await rsi.get_execution(later.execution_id)).instance_id == done.execution_id
    rows = (await rsi.get_task(started.task_id)).executions
    assert [row["instance_id"] for row in rows] == [done.execution_id] * 3


async def test_list_tasks_paginates_newest_first(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    ids = [(await rsi.create_task(key=str(i), objective="A")).task_id for i in range(3)]
    page = await rsi.list_tasks(limit=2)
    assert [item.task_id for item in page["items"]] == [ids[2], ids[1]] and page.has_more
    rest = await rsi.list_tasks(limit=2, cursor=page.next_cursor)
    assert [item.task_id for item in rest["items"]] == [ids[0]] and not rest.has_more


async def test_recall_memory_requires_a_client(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task = await rsi.create_task(key="k", objective="A")
    with pytest.raises(ValueError, match="requires a client"):
        await rsi.start_execution(task.task_id, key="e", source="new_task", objective="A", recall_memory=True)


async def test_context_changed_during_recall_is_refused(tmp_path):
    rsi = LocalRSI(client=None, model=FakeModel(), state_path=tmp_path / "s.sqlite3")
    task = await rsi.create_task(key="k", objective="Old")

    class Atoms:
        async def memory_recall(self, *, query, token_budget):
            await rsi.supplement_task_context(task.task_id, expected_context_version=1, objective="New")
            return {"items": []}

    rsi._atoms = Atoms()
    with pytest.raises(ValueError, match="changed while recalling"):
        await rsi.start_execution(task.task_id, key="e", source="new_task", objective="A", recall_memory=True)


async def test_unknown_ids_raise_lookup_error(tmp_path):
    rsi = LocalRSI(client=None, model=FakeModel(), state_path=tmp_path / "s.sqlite3")
    for call in (rsi.get_task("x"), rsi.get_execution("x"), rsi.get_revision("x"), rsi.get_artifact("x")):
        with pytest.raises(LookupError):
            await call


async def test_recall_asks_for_the_execution_input_with_the_8000_token_budget(tmp_path):
    rsi = LocalRSI(client=None, model=FakeModel(recipe_reply(), feedback_actionable=True), state_path=tmp_path / "s.sqlite3")
    task = await rsi.create_task(key="k", objective="Write the weekly report")
    calls = []

    class Atoms:
        async def memory_recall(self, *, query, token_budget):
            calls.append((query, token_budget))
            return {"items": []}

        async def upload_recipe(self, recipe_id, **_):
            return {}

    rsi._atoms = Atoms()
    started = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Week 3", recall_memory=True)
    assert calls == [("Week 3", 8000)]
    done = await complete_execution(rsi, started)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert calls == [("Week 3", 8000)] * 2  # the learning recalls for this instance, not the capability


async def test_a_start_key_belongs_to_one_execution_start(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task, started, done = await first_revision(rsi)
    other = await rsi.create_task(key="other", objective="B")
    with pytest.raises(ValueError, match="^idempotency key reused with different execution$"):
        await rsi.start_execution(other.task_id, key="t1-e", source="new_task", objective="Do the task")
    with pytest.raises(ValueError, match="^idempotency key reused with different execution$"):
        await rsi.start_revision(done.revision_id, key="t1-e")
    correction = await rsi.start_revision(done.revision_id, key="r")
    assert (await rsi.start_revision(done.revision_id, key="r")).execution_id == correction.execution_id
    with pytest.raises(ValueError, match="^idempotency key reused with different execution$"):
        await rsi.start_execution(other.task_id, key="r", source="new_task", objective="B")
    again = await complete_execution(rsi, correction, key="r-out")
    with pytest.raises(ValueError, match="^idempotency key reused with different execution$"):
        await rsi.start_revision(again.revision_id, key="r")
    assert [e["execution_id"] for e in (await rsi.get_task(task.task_id)).executions] == [
        started.execution_id, correction.execution_id]
    assert (await rsi.get_task(other.task_id)).executions == []
