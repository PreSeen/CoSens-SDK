import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cosens.errors import RSICandidateFailed
from cosens.local import LocalRSI, ModelUnavailable
from local_rsi_helpers import FakeModel, attribution_reply, complete_execution, first_revision, make_rsi, recipe_reply


async def generate(rsi, task, done):
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    return await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)


async def next_revision(rsi, task, key):
    started = await rsi.start_execution(task.task_id, key=key)
    return started, await complete_execution(rsi, started, key=key + "-out")


async def test_start_only_queues_and_wait_generates_version_one(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert candidate.status == "queued" and candidate.version == 1 and model.calls == []
    job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert job.status == "candidate" and job.current and job.recipe["title"] == "V1"
    assert job.models == {"attribution": None, "generation": "fake-model"} and job.memory_context == "not_requested"
    assert (await rsi.get_task_recipe(task.task_id)).current.version == 1


async def test_same_revision_reuses_the_candidate(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply())
    task, _, done = await first_revision(rsi)
    first = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    second = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id, key="other")
    assert first.recipe_candidate_id == second.recipe_candidate_id


async def test_next_execution_carries_the_recipe_and_v2_builds_on_it(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    started, second = await next_revision(rsi, task, "two")
    assert started.request["method"]["version"] == 1 and started.request["method"]["recipe"]["title"] == "V1"
    job = await generate(rsi, task, second)
    assert job.version == 2 and job.recipe["title"] == "V2"
    assert model.calls[1]["context"]["base_recipe"]["recipe"]["title"] == "V1"
    versions = await rsi.get_recipe_versions(job.recipe_id)
    assert [v.version for v in versions.versions] == [1, 2] and versions.current_candidate_id == job.recipe_candidate_id


async def test_unactionable_feedback_keeps_the_previous_version(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_reply(False))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await rsi.record_feedback(second.revision_id, feedback="thanks", source="human", key="f")
    job = await generate(rsi, task, second)
    assert job.status == "awaiting_material" and job.material_gap == "no_actionable_feedback"
    assert (await rsi.get_task_recipe(task.task_id)).current.recipe_candidate_id == v1.recipe_candidate_id


async def test_verified_new_evidence_makes_a_new_version(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_reply(False, verified_success=True, new_evidence=True), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await rsi.record_feedback(second.revision_id, feedback="Judge: correct", source="evaluator", key="f")
    job = await generate(rsi, task, second)
    assert job.status == "candidate" and job.version == 2 and job.attribution["verified_success"] is True


async def test_unverified_new_evidence_is_recorded_but_kept_out(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_reply(False, new_evidence=True))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await rsi.record_feedback(second.revision_id, feedback="looks fine", source="human", key="f")
    job = await generate(rsi, task, second)
    assert job.status == "awaiting_material" and job.material_gap == "unverified_new_evidence"
    assert job.attribution["new_evidence"]


async def test_actionable_feedback_runs_attribution_then_generation(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), attribution_reply(True), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await rsi.record_feedback(second.revision_id, feedback="wrong", source="evaluator", key="f")
    job = await generate(rsi, task, second)
    assert job.recipe["title"] == "V2" and job.models["attribution"] == "fake-model"
    assert [call["context"]["role"] for call in model.calls[1:]] == ["workflow_recipe_attribution", "workflow_recipe_improvement"]


async def test_model_failure_then_resume(tmp_path):
    rsi, _ = make_rsi(tmp_path, ModelUnavailable("down"), recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert caught.value.code == "upstream_unavailable" and caught.value.retryable and caught.value.phase == "generating"
    assert (await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)).status == "queued"
    assert (await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)).status == "candidate"


async def test_repeating_a_resume_returns_the_candidate(tmp_path):
    rsi, _ = make_rsi(tmp_path, ModelUnavailable("down"), recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    assert (await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)).status == "queued"
    await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert (await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)).status == "candidate"


async def test_attribution_survives_a_failed_generation(tmp_path):
    rsi, _ = make_rsi(tmp_path, attribution_reply(True), ModelUnavailable("down"))
    task, _, done = await first_revision(rsi)
    await rsi.record_feedback(done.revision_id, feedback="wrong", source="evaluator", key="f")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    view = await rsi.get_recipe_candidate(candidate.recipe_candidate_id)
    assert view.attribution["actionable"] is True and view.models["attribution"] == "fake-model"


async def test_wait_with_zero_timeout_returns_a_finished_candidate(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done)
    assert (await rsi.wait_workflow_job("recipe_candidate", job.recipe_candidate_id, timeout=0)).status == "candidate"


async def test_changed_stored_recipe_is_detected(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done)
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET recipe_json='{}'")
    with pytest.raises(ValueError, match="content changed"):
        await rsi.get_recipe_candidate(job.recipe_candidate_id)


async def test_invalid_output_fails_after_three_attempts(tmp_path):
    bad = {"title": "only"}
    rsi, model = make_rsi(tmp_path, bad, bad, bad)
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert caught.value.code == "model_output_invalid" and len(model.calls) == 3


async def test_failed_first_version_must_be_recovered_first(tmp_path):
    rsi, _ = make_rsi(tmp_path, ModelUnavailable("down"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    _, second = await next_revision(rsi, task, "two")
    with pytest.raises(ValueError, match="initial Recipe"):
        await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)


async def test_later_candidates_fail_with_parent_failed(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), ModelUnavailable("down"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    _, third = await next_revision(rsi, task, "three")
    middle = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    last = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    assert last.parent_candidate_id == middle.recipe_candidate_id
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", middle.recipe_candidate_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id)
    assert caught.value.code == "parent_failed"


async def test_a_resumed_child_waits_for_its_failed_parent(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), ModelUnavailable("down"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    _, third = await next_revision(rsi, task, "three")
    middle = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    last = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", middle.recipe_candidate_id)
    assert (await rsi.resume_recipe_candidate(last.recipe_candidate_id)).status == "awaiting_parent"
    with pytest.raises(TimeoutError):
        await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id, timeout=0.3)
    await rsi.resume_recipe_candidate(middle.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id)
    assert job.status == "candidate" and job.version == 3


async def test_waiting_on_a_later_candidate_generates_its_parent_first(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    _, second = await next_revision(rsi, task, "two")
    last = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    job = await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id)
    assert job.version == 2 and len(model.calls) == 2


async def test_commit_refuses_when_the_parent_is_no_longer_current(tmp_path):
    class ChangesHead(FakeModel):
        async def call(self, **kwargs):
            with rsi._store.transaction() as db:
                db.execute("UPDATE recipe_heads SET current_candidate_id='other'")
            return await super().call(**kwargs)

    model = ChangesHead(recipe_reply("V1"))
    rsi = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert caught.value.code == "parent_changed" and caught.value.retryable is False


async def test_wait_timeout_leaves_generation_running(tmp_path):
    class Slow(FakeModel):
        async def call(self, **kwargs):
            await asyncio.sleep(0.5)
            return await super().call(**kwargs)

    model = Slow(recipe_reply("V1"))
    rsi = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(TimeoutError):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id, timeout=0.1)
    assert (await rsi.get_recipe_candidate(candidate.recipe_candidate_id)).status == "running"
    generating = rsi._settling[candidate.recipe_candidate_id]
    assert rsi._settling_task(candidate.recipe_candidate_id) is generating
    assert (await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)).status == "candidate"
    assert len(model.calls) == 1
    await asyncio.sleep(0)
    assert rsi._settling == {}


async def test_aclose_returns_a_running_generation_to_the_queue(tmp_path):
    class Slow(FakeModel):
        async def call(self, **kwargs):
            await asyncio.sleep(30)

    path = tmp_path / "s.sqlite3"
    rsi = LocalRSI(client=None, model=Slow(), state_path=path)
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(TimeoutError):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id, timeout=0.1)
    await rsi.aclose()
    reopened = LocalRSI(client=None, model=FakeModel(), state_path=path)
    assert (await reopened.get_recipe_candidate(candidate.recipe_candidate_id)).status == "queued"


async def test_changed_parent_recipe_fails_the_candidate(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    child = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET recipe_json='{}' WHERE id=?", (child.parent_candidate_id,))
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", child.recipe_candidate_id)
    assert caught.value.code == "internal_error"


async def test_one_bad_row_does_not_break_the_list(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    with rsi._store.transaction() as db:
        db.execute("""UPDATE recipe_candidates SET recipe_json='{"title": "changed"}'""")
    assert (await rsi.list_recipe_candidates())["items"][0]["title"] == "changed"


async def test_a_process_without_a_client_leaves_a_pending_upload_alone(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done)
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET publish_state='pending'")
    assert (await rsi.wait_workflow_job("recipe_candidate", job.recipe_candidate_id)).publish_state == "pending"


async def test_cancelled_generation_goes_back_to_the_queue(tmp_path):
    class Slow(FakeModel):
        async def call(self, **kwargs):
            await asyncio.sleep(30)

    rsi = LocalRSI(client=None, model=Slow(), state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(TimeoutError):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id, timeout=0.1)
    running = rsi._settling[candidate.recipe_candidate_id]
    running.cancel()
    await asyncio.wait({running})
    await asyncio.sleep(0)
    assert (await rsi.get_recipe_candidate(candidate.recipe_candidate_id)).status == "queued"


async def test_expired_lease_becomes_interrupted_and_can_resume(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET state='running',lease_expires_at=0")
    job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert job.status == "interrupted"
    await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    assert (await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)).status == "candidate"


async def test_stale_generation_cannot_commit_after_resume(tmp_path):
    class ResumedMeanwhile(FakeModel):
        async def call(self, **kwargs):
            with rsi._store.transaction() as db:
                db.execute("UPDATE recipe_candidates SET generation=generation+1")
            return await super().call(**kwargs)

    model = ResumedMeanwhile(recipe_reply("V1"))
    rsi = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    stale = asyncio.create_task(rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id, timeout=1))
    with pytest.raises(TimeoutError):
        await stale
    view = await rsi.get_recipe_candidate(candidate.recipe_candidate_id)
    assert view.status == "running" and view.recipe is None
    rsi._settling[candidate.recipe_candidate_id].cancel()


async def test_an_interrupted_parent_blocks_until_resumed(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    parent = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET state='running',lease_expires_at=0")
    assert (await rsi.wait_workflow_job("recipe_candidate", parent.recipe_candidate_id)).status == "interrupted"
    _, second = await next_revision(rsi, task, "two")
    child = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    assert child.parent_candidate_id == parent.recipe_candidate_id
    with pytest.raises(TimeoutError):
        await rsi.wait_workflow_job("recipe_candidate", child.recipe_candidate_id, timeout=0.3)
    await rsi.resume_recipe_candidate(parent.recipe_candidate_id)
    assert (await rsi.wait_workflow_job("recipe_candidate", child.recipe_candidate_id)).version == 2


async def test_two_connections_generate_once(tmp_path):
    class Slow(FakeModel):
        async def call(self, **kwargs):
            await asyncio.sleep(0.3)
            return await super().call(**kwargs)

    model = Slow(recipe_reply("V1"))
    first = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    second = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(first)
    candidate = await first.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    jobs = await asyncio.gather(*(rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id) for rsi in (first, second)))
    assert [job.status for job in jobs] == ["candidate", "candidate"] and len(model.calls) == 1


async def test_two_processes_generate_once(tmp_path):
    rsi, _ = make_rsi(tmp_path, name="shared.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    calls = tmp_path / "calls.txt"
    script = textwrap.dedent(f"""\
        import asyncio
        from cosens.local import LocalRSI
        from local_rsi_helpers import FakeModel, recipe_reply

        class Counted(FakeModel):
            async def call(self, **kwargs):
                with open({str(calls)!r}, "a") as out:
                    out.write("call\\n")
                await asyncio.sleep(0.5)
                return await super().call(**kwargs)

        async def main():
            rsi = LocalRSI(client=None, model=Counted(recipe_reply("V1")), state_path={str(tmp_path / "shared.sqlite3")!r})
            job = await rsi.wait_workflow_job("recipe_candidate", {candidate.recipe_candidate_id!r}, timeout=30)
            print(job.status)

        asyncio.run(main())
    """)
    env = {**os.environ, "PYTHONPATH": f"{Path(__file__).parent}{os.pathsep}{Path(__file__).parents[1] / 'src'}"}
    processes = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=env, text=True) for _ in range(2)]
    outputs = [p.communicate(timeout=60)[0].strip() for p in processes]
    assert outputs == ["candidate", "candidate"]
    assert calls.read_text().count("call") == 1


async def test_context_over_the_limit_is_rejected(tmp_path, monkeypatch):
    from cosens.local import recipes
    monkeypatch.setattr(recipes, "MAX_CONTEXT_BYTES", 10)
    rsi, _ = make_rsi(tmp_path)
    task, _, done = await first_revision(rsi)
    with pytest.raises(ValueError, match="4 MB"):
        await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)


async def test_history_is_limited_to_thirty_revisions(tmp_path, monkeypatch):
    from cosens.local import recipes
    monkeypatch.setattr(recipes, "MAX_HISTORY_REVISIONS", 1)
    rsi, _ = make_rsi(tmp_path)
    task, _, done = await first_revision(rsi)
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    with pytest.raises(ValueError, match="excessive"):
        await rsi.start_recipe_candidate(task.task_id, revision_id=again.revision_id)


async def test_history_follows_the_revision_chain_with_feedback(tmp_path):
    rsi, model = make_rsi(tmp_path, attribution_reply(True), recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    await rsi.record_feedback(done.revision_id, feedback="wrong", source="human", key="f")
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    await generate(rsi, task, again)
    history = model.calls[0]["context"]["context"]["history"]
    assert [h["revision_id"] for h in history] == [done.revision_id, again.revision_id]
    assert history[0]["feedback"][0]["message"] == "wrong"


async def test_list_recipe_candidates_filters_and_omits_full_recipe(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    page = await rsi.list_recipe_candidates(status="candidate", task_id=task.task_id)
    assert page["items"][0]["title"] == "V1" and "recipe" not in page["items"][0]
    assert (await rsi.list_recipe_candidates(status="failed"))["items"] == []
    with pytest.raises(ValueError):
        await rsi.list_recipe_candidates(status="nonsense")


async def test_wait_rejects_other_job_kinds_and_bad_timeouts(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    with pytest.raises(ValueError, match="job kind"):
        await rsi.wait_workflow_job("memory", "x")
    with pytest.raises(ValueError, match="timeout"):
        await rsi.wait_workflow_job("recipe_candidate", "x", timeout=float("nan"))


