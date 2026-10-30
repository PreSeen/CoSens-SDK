"""New task-workflow atoms are exact, single-request HTTP adapters."""

import json

import httpx
import pytest

from cosens import CoSensClient


@pytest.mark.asyncio
async def test_workflow_atoms_cover_all_routes_and_wire_bodies():
    seen = []

    def server(request):
        seen.append((request.method, request.url.raw_path.decode().split("?")[0], json.loads(request.content) if request.content else None,
                     request.headers.get("idempotency-key"), request.headers.get("x-auth-type"), request.headers.get("authorization")))
        return httpx.Response(200, json={"status": "queued"})

    client = CoSensClient(api_key="example-key", _http_transport=httpx.MockTransport(server))
    atoms = client.atoms
    expected = [
        ("GET", "/api/v1/artifacts/a%2Fb", None, None),
        ("POST", "/api/v1/tasks", {"source": "new_task", "artifacts": ["a1"], "objective": "work"}, "task-key"),
        ("GET", "/api/v1/tasks/t1", None, None),
        ("POST", "/api/v1/tasks/t1/context", {"expected_context_version": 1, "self_contained": True}, None),
        ("POST", "/api/v1/tasks/t1/analysis", None, "analysis-key"),
        ("GET", "/api/v1/task-analyses/an1", None, None),
        ("POST", "/api/v1/task-analyses/an1/resume", None, None),
        ("POST", "/api/v1/tasks/t1/executions", None, "execution-key"),
        ("POST", "/api/v1/tasks/t1/reconstructions", None, "reconstruction-key"),
        ("GET", "/api/v1/executions/ex1", None, None),
        ("POST", "/api/v1/executions/ex1/result", {"request_sha256": "hash1", "outcome": "completed", "artifacts": [{"artifact_id": "a2", "role": "submitted_output"}], "events": [], "trace_coverage": "unknown"}, None),
        ("GET", "/api/v1/revisions/r1", None, None),
        ("POST", "/api/v1/revisions/r1/attributions", {"data": {}}, "attribution-key"),
        ("GET", "/api/v1/attributions/at1", None, None),
        ("POST", "/api/v1/attributions/at1/resume", None, None),
        ("POST", "/api/v1/attributions/at1/selection", {"data": {"candidate_sha256": "hash2", "custom_reason": "reason"}}, None),
        ("POST", "/api/v1/revisions/r1/revisions", {"data": {"attribution_id": "at1"}}, "revision-key"),
        ("POST", "/api/v1/revisions/r1/acceptance", {"data": {"artifact_hashes": {"a2": "server-hash"}}}, None),
        ("POST", "/api/v1/tasks/t1/recipe-candidates", {"data": {"revision_id": "r1", "include_memory": False}}, "recipe-key"),
        ("GET", "/api/v1/recipe-candidates/rc1", None, None),
        ("POST", "/api/v1/recipe-candidates/rc1/resume", None, None),
        ("POST", "/api/v1/recipe-candidates/rc1/verifications", None, "verification-key"),
        ("GET", "/api/v1/recipe-verifications/v1", None, None),
    ]
    await atoms.get_artifact("a/b")
    await atoms.create_task(source="new_task", objective="work", artifacts=["a1"], key="task-key")
    await atoms.get_task("t1")
    await atoms.supplement_task_context("t1", expected_context_version=1, self_contained=True)
    await atoms.start_external_analysis("t1", key="analysis-key")
    await atoms.get_external_analysis("an1")
    await atoms.resume_external_analysis("an1")
    await atoms.start_execution("t1", key="execution-key")
    await atoms.start_reconstruction("t1", key="reconstruction-key")
    await atoms.get_execution("ex1")
    await atoms.submit_execution("ex1", request_sha256="hash1", outcome="completed", artifacts=[{"artifact_id": "a2", "role": "submitted_output"}])
    await atoms.get_revision("r1")
    await atoms.start_attribution("r1", key="attribution-key")
    await atoms.get_attribution("at1")
    await atoms.resume_attribution("at1")
    await atoms.select_cause("at1", candidate_sha256="hash2", custom_reason="reason")
    await atoms.start_revision("r1", attribution_id="at1", key="revision-key")
    await atoms.accept_revision("r1", artifact_hashes={"a2": "server-hash"})
    await atoms.start_recipe_candidate("t1", revision_id="r1", key="recipe-key")
    await atoms.get_recipe_candidate("rc1")
    await atoms.resume_recipe_candidate("rc1")
    await atoms.start_verification("rc1", key="verification-key")
    await atoms.get_verification("v1")
    assert [item[:4] for item in seen] == expected
    assert all(item[4:] == ("api_key", "Bearer example-key") for item in seen)
    await client.aclose()


@pytest.mark.asyncio
async def test_workflow_atoms_reject_invalid_local_requests_without_network_and_expose_sync_form():
    seen = []
    client = CoSensClient(api_key="example-key", _http_transport=httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(200, json={}))[1]))
    with pytest.raises(ValueError):
        await client.atoms.start_execution("t1", key="")
    with pytest.raises(ValueError):
        await client.atoms.select_cause("at1", candidate_sha256="hash", candidate_id="c1", custom_reason="other")
    with pytest.raises(ValueError):
        await client.atoms.submit_execution("ex1", request_sha256="hash", outcome="completed", artifacts=[])
    with pytest.raises(ValueError):
        await client.atoms.supplement_task_context("t1", expected_context_version=0)
    assert seen == []
    client.sync_atoms.start_verification("rc1", key="key-1")
    assert seen[0].url.path == "/api/v1/recipe-candidates/rc1/verifications"
    assert seen[0].headers["idempotency-key"] == "key-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_workflow_job_polling_stops_on_waiting_runner_and_rejects_invalid_timeout():
    seen = []

    def server(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "awaiting_runner", "analysis_id": "an1"})

    async with CoSensClient(api_key="key", _http_transport=httpx.MockTransport(server)) as client:
        with pytest.raises(ValueError, match="timeout"):
            await client.atoms.wait_workflow_job("analysis", "an1", timeout=float("nan"))
        job = await client.atoms.wait_workflow_job("analysis", "an1", timeout=0)
        sync_job = client.sync_atoms.wait_workflow_job("analysis", "an1", timeout=0)
    assert job.status == sync_job.status == "awaiting_runner"
    assert seen == ["/api/v1/task-analyses/an1", "/api/v1/task-analyses/an1"]


@pytest.mark.asyncio
async def test_workflow_atoms_reject_conflicting_artifact_roles_and_oversized_result_before_network():
    seen = []
    client = CoSensClient(api_key="key", _http_transport=httpx.MockTransport(lambda request: (seen.append(request), httpx.Response(200, json={}))[1]))
    with pytest.raises(ValueError, match="both original_input and submitted_output"):
        await client.atoms.create_task(source="new_task", objective="work", key="k1", artifacts=[
            {"artifact_id": "same", "role": "original_input"}, {"artifact_id": "same", "role": "submitted_output"}])
    with pytest.raises(ValueError, match="at most 1,000,000 bytes"):
        await client.atoms.submit_execution("e1", request_sha256="hash", outcome="completed",
            artifacts=[{"artifact_id": "a1", "role": "submitted_output"}],
            events=[{"id": "event-1", "kind": "tool_result", "payload": {"large": "x" * 1_000_000}}])
    assert seen == []
    await client.aclose()
