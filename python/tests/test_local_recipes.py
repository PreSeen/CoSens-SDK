import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from cosens import RSIFeedbackRequired
from cosens.local.recipes import RSICandidateFailed
from cosens.local import LocalRSI, ModelUnavailable
from cosens.local.store import canonical_json
from local_rsi_helpers import FakeModel, attribution_reply, complete_execution, first_revision, make_rsi, recipe_reply


async def generate(rsi, task, done):
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    return await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)


async def next_revision(rsi, task, key):
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
    return started, await complete_execution(rsi, started, key=key + "-out")


async def test_start_only_queues_and_wait_generates_version_one(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert candidate.status == "queued" and candidate.version == 1 and model.calls == []
    job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert job.status == "candidate" and job.current and job.recipe["title"] == "V1"
    assert job.models == {"attribution": "fake-model", "generation": "fake-model"} and job.memory_context == "not_requested"
    assert (await rsi.get_task_recipe(task.task_id)).current.version == 1


async def unfed_revision(rsi, task, key):
    """A completed execution with no feedback recorded on it."""
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
    return await complete_execution(rsi, started, key=key + "-out", feedback=None)


async def test_learning_without_feedback_is_refused_and_leaves_nothing(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"))
    task = await rsi.create_task(key="t1", objective="Do the task")
    done = await unfed_revision(rsi, task, "one")
    with pytest.raises(RSIFeedbackRequired) as caught:
        await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert (caught.value.status_code, caught.value.code) == (None, "feedback_required")
    assert caught.value.allowed_sources == ["human", "evaluator", "reference", "automated_check", "downstream"]
    counts = [rsi._store.read_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
              for table in ("recipe_candidates", "recipe_versions", "recipe_heads", "task_recipes")]
    assert counts == [0, 0, 0, 0] and model.calls == []
    assert rsi._store.read_one("SELECT COUNT(*) AS n FROM idempotency WHERE operation='recipe_candidate'")["n"] == 0
    await rsi.record_feedback(done.revision_id, feedback="Keep this", source="human", key="f")
    job = await generate(rsi, task, done)
    assert (job.status, job.version) == ("candidate", 1)


async def test_a_refused_learning_takes_no_version_number(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    second = await unfed_revision(rsi, task, "two")
    with pytest.raises(RSIFeedbackRequired):
        await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    recipe = await rsi.get_task_recipe(task.task_id)
    assert recipe.current.recipe_candidate_id == recipe.latest.recipe_candidate_id == v1.recipe_candidate_id
    await rsi.record_feedback(second.revision_id, feedback="Judge: too long", source="evaluator", key="f2")
    assert (await generate(rsi, task, second)).version == 2


async def test_feedback_on_an_earlier_draft_lets_a_correction_learn(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out", feedback=None)
    assert (await generate(rsi, task, again)).version == 1


async def test_feedback_stored_without_a_source_counts(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task = await rsi.create_task(key="t1", objective="Do the task")
    done = await unfed_revision(rsi, task, "one")
    legacy = {"feedback_id": "feedback_legacy", "revision_id": done.revision_id, "task_id": task.task_id, "message": "Keep it"}
    with rsi._store.transaction() as db:
        db.execute("INSERT INTO feedback VALUES(?,?,?,?)", ("feedback_legacy", done.revision_id, "legacy", canonical_json(legacy)))
    assert (await generate(rsi, task, done)).version == 1


def move_current(rsi, candidate_id):
    """Move the chain's current version back, as following a rollback on the service does."""
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_heads SET current_candidate_id=?", (candidate_id,))


def interrupt(rsi, candidate_id):
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET state='interrupted' WHERE id=?", (candidate_id,))


async def test_after_a_rollback_the_next_learning_builds_on_the_current_version(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await generate(rsi, task, second)
    move_current(rsi, v1.recipe_candidate_id)
    _, third = await next_revision(rsi, task, "three")
    v3 = await generate(rsi, task, third)
    assert (v3.version, v3.parent_candidate_id, v3.current) == (3, v1.recipe_candidate_id, True)
    assert model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "V1"


async def test_a_learning_queued_on_a_rolled_back_version_is_not_followed(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    v2 = await generate(rsi, task, second)
    _, third = await next_revision(rsi, task, "three")
    doomed = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    assert doomed.parent_candidate_id == v2.recipe_candidate_id
    move_current(rsi, v1.recipe_candidate_id)
    _, fourth = await next_revision(rsi, task, "four")
    fresh = await rsi.start_recipe_candidate(task.task_id, revision_id=fourth.revision_id)
    assert (fresh.version, fresh.parent_candidate_id) == (4, v1.recipe_candidate_id)


async def test_an_interrupted_latest_is_not_waited_for_when_a_version_is_current(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V3"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    stopped = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    interrupt(rsi, stopped.recipe_candidate_id)
    _, third = await next_revision(rsi, task, "three")
    fresh = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    assert (fresh.version, fresh.parent_candidate_id) == (3, v1.recipe_candidate_id)
    await rsi.wait_workflow_job("recipe_candidate", fresh.recipe_candidate_id)
    await rsi.resume_recipe_candidate(stopped.recipe_candidate_id)
    resumed = await rsi.wait_workflow_job("recipe_candidate", stopped.recipe_candidate_id)
    assert (resumed.version, resumed.parent_candidate_id, resumed.current) == (4, fresh.recipe_candidate_id, True)


async def test_with_no_current_version_a_learning_queues_behind_an_interrupted_first_version(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    task, _, done = await first_revision(rsi)
    first = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    interrupt(rsi, first.recipe_candidate_id)
    _, second = await next_revision(rsi, task, "two")
    queued = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    assert (queued.version, queued.parent_candidate_id) == (2, first.recipe_candidate_id)


async def test_a_resumed_candidate_leaves_an_in_flight_parent_on_a_rolled_back_version(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"), recipe_reply("Again"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await generate(rsi, task, second)
    _, third = await next_revision(rsi, task, "three")
    doomed = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    _, fourth = await next_revision(rsi, task, "four")
    child = await rsi.start_recipe_candidate(task.task_id, revision_id=fourth.revision_id)
    assert child.parent_candidate_id == doomed.recipe_candidate_id
    interrupt(rsi, child.recipe_candidate_id)
    move_current(rsi, v1.recipe_candidate_id)
    await rsi.resume_recipe_candidate(child.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", child.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.current) == (5, v1.recipe_candidate_id, True)


async def test_an_in_flight_child_does_not_pin_a_resumed_candidate_to_a_rolled_back_version(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    await generate(rsi, task, second)
    _, third = await next_revision(rsi, task, "three")
    stopped = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    _, fourth = await next_revision(rsi, task, "four")
    await rsi.start_recipe_candidate(task.task_id, revision_id=fourth.revision_id)  # queued behind it
    interrupt(rsi, stopped.recipe_candidate_id)
    move_current(rsi, v1.recipe_candidate_id)
    await rsi.resume_recipe_candidate(stopped.recipe_candidate_id)
    view = await rsi.get_recipe_candidate(stopped.recipe_candidate_id)
    assert (view.version, view.parent_candidate_id, view.status) == (5, v1.recipe_candidate_id, "queued")


async def test_an_interrupted_candidate_resumes_behind_a_newer_one_although_its_child_waits_for_it(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), recipe_reply("V4"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    stopped = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    _, third = await next_revision(rsi, task, "three")
    child = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    assert child.parent_candidate_id == stopped.recipe_candidate_id
    interrupt(rsi, stopped.recipe_candidate_id)
    _, fourth = await next_revision(rsi, task, "four")
    fresh = await rsi.start_recipe_candidate(task.task_id, revision_id=fourth.revision_id)
    assert (fresh.version, fresh.parent_candidate_id) == (4, v1.recipe_candidate_id)
    # The child waits on the interrupted candidate, but it does not keep it on V1, where it would race the new V4.
    await rsi.resume_recipe_candidate(stopped.recipe_candidate_id)
    await rsi.wait_workflow_job("recipe_candidate", fresh.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", stopped.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.current) == (5, fresh.recipe_candidate_id, True)


async def test_rolled_back_follows_the_current_versions_ancestors(tmp_path):
    rsi, _ = make_rsi(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, _, done = await first_revision(rsi)
    versions = [await generate(rsi, task, done)]
    for key in ("two", "three"):
        _, revision = await next_revision(rsi, task, key)
        versions.append(await generate(rsi, task, revision))

    async def flags():
        return [(item.version, item.rolled_back) for item in (await rsi.get_recipe_versions(versions[0].recipe_id)).versions]

    move_current(rsi, versions[0].recipe_candidate_id)
    assert await flags() == [(1, False), (2, True), (3, True)]
    _, fourth = await next_revision(rsi, task, "four")
    versions.append(await generate(rsi, task, fourth))
    assert await flags() == [(1, False), (2, True), (3, True), (4, False)]
    move_current(rsi, versions[2].recipe_candidate_id)
    assert await flags() == [(1, False), (2, False), (3, False), (4, True)]


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


async def test_a_resumed_child_waits_for_a_parent_resumed_first(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), ModelUnavailable("down"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    _, third = await next_revision(rsi, task, "three")
    middle = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    last = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", middle.recipe_candidate_id)
    assert (await rsi.resume_recipe_candidate(middle.recipe_candidate_id)).status == "queued"
    # Its parent is in flight again, so the child stays behind it with its number.
    assert (await rsi.resume_recipe_candidate(last.recipe_candidate_id)).status == "awaiting_parent"
    assert (await rsi.get_task(task.task_id)).recipes[0]["learning"]["status"] == "awaiting_previous_version"
    view = await rsi.get_recipe_candidate(last.recipe_candidate_id)
    assert (view.version, view.parent_candidate_id) == (3, middle.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id)
    assert job.status == "candidate" and job.version == 3 and job.recipe["title"] == "V3"


async def test_a_child_resumed_while_its_parent_is_failed_builds_on_the_current_version(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), ModelUnavailable("down"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    _, third = await next_revision(rsi, task, "three")
    middle = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    last = await rsi.start_recipe_candidate(task.task_id, revision_id=third.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", middle.recipe_candidate_id)
    # As on the service, a resume never waits on a failed parent: it moves behind the current version, numbered above it.
    assert (await rsi.resume_recipe_candidate(last.recipe_candidate_id)).status == "queued"
    job = await rsi.wait_workflow_job("recipe_candidate", last.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.current) == (4, v1.recipe_candidate_id, True)
    assert model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "V1"
    await rsi.resume_recipe_candidate(middle.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", middle.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.current) == (5, last.recipe_candidate_id, True)
    versions = await rsi.get_recipe_versions(job.recipe_id)
    assert [(v.version, v.status) for v in versions.versions] == [(1, "candidate"), (4, "candidate"), (5, "candidate")]


async def test_a_transient_failure_resumes_once_on_the_version_that_became_current(tmp_path):
    rsi, model = make_rsi(tmp_path, recipe_reply("V1"), ModelUnavailable("down"), recipe_reply("V3"), recipe_reply("V4"))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done)
    _, second = await next_revision(rsi, task, "two")
    _, third = await next_revision(rsi, task, "three")
    failed = await rsi.start_recipe_candidate(task.task_id, revision_id=second.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", failed.recipe_candidate_id)
    v3 = await generate(rsi, task, third)
    assert (v3.version, v3.parent_candidate_id) == (3, v1.recipe_candidate_id)
    calls = len(model.calls)
    resumed = await rsi.resume_recipe_candidate(failed.recipe_candidate_id)
    assert resumed.status == "queued"
    job = await rsi.wait_workflow_job("recipe_candidate", failed.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.current) == (4, v3.recipe_candidate_id, True)
    assert len(model.calls) == calls + 1 and model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "V3"


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

    model = ChangesHead(recipe_reply("V1"), feedback_actionable=True)
    rsi = LocalRSI(client=None, model=model, state_path=tmp_path / "s.sqlite3")
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert caught.value.code == "parent_changed" and caught.value.retryable is True


async def test_wait_timeout_leaves_generation_running(tmp_path):
    class Slow(FakeModel):
        async def call(self, **kwargs):
            await asyncio.sleep(0.5)
            return await super().call(**kwargs)

    model = Slow(recipe_reply("V1"), feedback_actionable=True)
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

    model = Slow(recipe_reply("V1"), feedback_actionable=True)
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
            rsi = LocalRSI(client=None, model=Counted(recipe_reply("V1"), feedback_actionable=True), state_path={str(tmp_path / "shared.sqlite3")!r})
            job = await rsi.wait_workflow_job("recipe_candidate", {candidate.recipe_candidate_id!r}, timeout=30)
            print(job.status)

        asyncio.run(main())
    """)
    env = {**os.environ, "PYTHONPATH": f"{Path(__file__).parent}{os.pathsep}{Path(__file__).parents[1] / 'src'}"}
    processes = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=env, text=True) for _ in range(2)]
    outputs = [p.communicate(timeout=60)[0].strip() for p in processes]
    assert outputs == ["candidate", "candidate"]
    assert calls.read_text().count("call") == 2  # attribution and generation, in one process


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
    context = model.calls[0]["context"]["context"]
    history = context["history"]
    assert [h["execution_id"] for h in history] == [done.execution_id, again.execution_id]
    assert [h["origin"] for h in history] == ["original", "correction"]
    assert context["source_execution_id"] == again.execution_id and "source_revision_id" not in context
    assert [item["message"] for item in history[0]["feedback"]] == ["Keep this method", "wrong"]
    assert history[0]["feedback"][1]["execution_id"] == done.execution_id and "revision_id" not in history[0]["feedback"][1]
    assert all("revision_id" not in entry for entry in history)


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




async def test_the_task_shows_which_recipe_is_learning(tmp_path):
    rsi, _ = make_rsi(tmp_path, ModelUnavailable("down"), recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    # The first learning creates the Recipe; it has no current version yet and is being iterated.
    [recipe] = (await rsi.get_task(task.task_id)).recipes
    assert recipe == {"recipe_id": candidate.recipe_id, "current_version": None, "latest_version": 1,
                      "learning": {"execution_id": done.execution_id, "version": 1, "status": "queued",
                                   "phase": "queued", "error": None}}
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    [recipe] = (await rsi.get_task(task.task_id)).recipes
    assert recipe["learning"]["status"] == "failed" and recipe["learning"]["error"] == "upstream_unavailable"
    await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    [recipe] = (await rsi.get_task(task.task_id)).recipes
    assert recipe == {"recipe_id": candidate.recipe_id, "current_version": 1, "latest_version": 1,
                      "learning": {"execution_id": done.execution_id, "version": 1, "status": "completed",
                                   "phase": "completed", "error": None}}
    started, second = await next_revision(rsi, task, "two")
    assert started.request["method"]["recipe_id"] == candidate.recipe_id
    await generate(rsi, task, second)
    [recipe] = (await rsi.get_task(task.task_id)).recipes
    assert (recipe["current_version"], recipe["latest_version"]) == (2, 2)
    assert recipe["learning"]["execution_id"] == second.execution_id and recipe["learning"]["version"] == 2


async def test_a_recipe_belongs_to_its_task(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done)
    named = await rsi.start_execution(task.task_id, key="named", source="new_task", objective="Again", recipe_id=job.recipe_id)
    assert named.request["method"]["recipe_id"] == job.recipe_id
    other = await rsi.create_task(key="other", objective="Assess quarterly performance")
    with pytest.raises(LookupError, match="not found in this task"):
        await rsi.start_execution(other.task_id, key="o", source="new_task", objective="Q4", recipe_id=job.recipe_id)
    assert (await rsi.get_task(other.task_id)).recipes == []


async def test_a_named_recipe_without_a_usable_version_is_refused(tmp_path):
    rsi, _ = make_rsi(tmp_path, ModelUnavailable("down"))
    task, _, done = await first_revision(rsi)
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    with pytest.raises(ValueError, match="^Recipe has no usable version yet$"):
        await rsi.start_execution(task.task_id, key="named", source="new_task", objective="Again",
                                  recipe_id=candidate.recipe_id)
    unnamed = await rsi.start_execution(task.task_id, key="unnamed", source="new_task", objective="Again")
    assert "method" not in unnamed.request


async def test_a_task_with_several_recipes_needs_a_recipe_choice(tmp_path):
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_reply(True), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    _, early = await next_revision(rsi, task, "early")  # follows no Recipe: the task has none yet
    job = await generate(rsi, task, done)
    with rsi._store.transaction() as db:  # delivery 1 has no way to add a second Recipe; write one directly
        candidate = db.execute("SELECT * FROM recipe_candidates WHERE id=?", (job.recipe_candidate_id,)).fetchone()
        db.execute("INSERT INTO recipe_candidates(id,task_id,revision_id,state,phase,context_json,context_sha,recipe_json,"
                   "recipe_sha) VALUES('second',?,NULL,'candidate','completed',?,?,?,?)",
                   (task.task_id, candidate["context_json"], candidate["context_sha"], candidate["recipe_json"],
                    candidate["recipe_sha"]))
        db.execute("INSERT INTO recipe_heads(recipe_id,current_candidate_id,latest_candidate_id) VALUES('second','second','second')")
        db.execute("INSERT INTO recipe_versions VALUES('second','second',1,NULL)")
        db.execute("INSERT INTO task_recipes VALUES(?,'second')", (task.task_id,))
    with pytest.raises(ValueError, match="^the task has several Recipes; name one with recipe_id$"):
        await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Again")
    chosen = await rsi.start_execution(task.task_id, key="e", source="new_task", objective="Again", recipe_id="second")
    assert chosen.request["method"]["recipe_id"] == "second"
    assert [r["recipe_id"] for r in (await rsi.get_task(task.task_id)).recipes] == sorted([job.recipe_id, "second"])
    [imported] = [r for r in (await rsi.get_task(task.task_id)).recipes if r["recipe_id"] == "second"]
    assert imported["learning"] is None  # its newest version was not learned from an execution
    # A correction, and a learning from it, stay with the Recipe the execution followed.
    done = await complete_execution(rsi, chosen, key="e-out")
    await rsi.record_feedback(done.revision_id, feedback="wrong", source="human", key="f")
    correction = await rsi.start_revision(done.revision_id, key="r")
    assert correction.request["method"]["recipe_id"] == "second"
    again = await complete_execution(rsi, correction, key="r-out")
    learned = await generate(rsi, task, again)
    assert (learned.recipe_id, learned.version) == ("second", 2)
    with pytest.raises(ValueError, match="^the execution followed no Recipe and the task has several$"):
        await rsi.start_recipe_candidate(task.task_id, revision_id=early.revision_id)
    # A correction cannot name a Recipe, so the refusal does not ask for a recipe_id.
    with pytest.raises(ValueError, match="^the previous execution followed no Recipe and the task has several$"):
        await rsi.start_revision(early.revision_id, key="early-r")


def recipe_citing_the_template(payload):
    """A Recipe whose step cites the task's reference material."""
    [template] = payload["context"]["task_materials"]
    return {"title": "Weekly report", "when": "always", "when_not": "never", "lessons": [],
            "steps": [{"id": "s1", "instruction": "follow the template",
                       "evidence_refs": [{"artifact_id": template["artifact_id"]}]}]}


async def test_learning_freezes_the_task_context_and_each_input(tmp_path):
    rsi, model = make_rsi(tmp_path, attribution_reply(True), recipe_citing_the_template)
    template = await rsi.artifact(name="template.md", content="## Risks")
    task = await rsi.create_task(key="t", objective="Write the weekly report", artifacts=[template.artifact_id])
    started = await rsi.start_execution(task.task_id, key="w1", source="new_task", objective="Week 1")
    done = await complete_execution(rsi, started, key="w1-out")
    await rsi.record_feedback(done.revision_id, feedback="Add risks", source="human", key="f")
    correction = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, correction, key="r-out")
    learned = await generate(rsi, task, again)
    # Evidence may cite the task's reference material as well as the history.
    assert learned.status == "candidate"
    assert learned.recipe["steps"][0]["evidence_refs"] == [{"artifact_id": template.artifact_id}]
    context = model.calls[0]["context"]["context"]
    assert context["schema"] == "workflow-recipe-context-v3"
    assert context["task_context"] == {"objective": "Write the weekly report", "context_version": 1,
                                       "artifacts": [dict(a) for a in task.artifacts]}
    assert [m["content"] for m in context["task_materials"]] == ["## Risks"]
    assert [h["input"]["objective"] for h in context["history"]] == ["Week 1", "Week 1"]
    assert all("context" not in h for h in context["history"])
