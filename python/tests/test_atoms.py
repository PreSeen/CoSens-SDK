import asyncio

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError


@pytest.mark.asyncio
async def test_atomic_endpoints_send_the_documented_requests():
    requests = []

    def server(request):
        import json

        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, dict(request.headers), body))
        payloads = {
            "/api/v1/artifacts": {"artifact_id": "a1", "content_hash": "h1", "name": "input.txt", "size_bytes": 5},
            "/api/v1/runs/prepare": {"run_id": "r1", "status": "ready", "recipe": {}, "context": {}},
            "/api/v1/runs/r1": {"run_id": "r1", "status": "ready", "snapshot": {}},
            "/api/v1/runs/r1/session-events": {"durable_ack": ["e1"]},
            "/api/v1/runs/r1/initial-inputs": {"run_id": "r1", "status": "ready"},
            "/api/v1/runs/r1/submit": {"run_id": "r1", "status": "completed", "evaluation": "inconclusive", "accepted": None},
            "/api/v1/runs/r1/feedback": {"feedback_id": "f1", "status": "recorded"},
            "/api/v1/runs/r1/improvements": {"job_id": "j1", "status": "queued"},
            "/api/v1/improvements/j1": {"job_id": "j1", "status": "validated"},
            "/api/v1/improvements/j1/resume": {"job_id": "j1", "status": "queued"},
            "/api/internal/admin/v1/improvements/j1/publish": {"release_id": "rel1", "status": "published"},
        }
        return httpx.Response(200, json=payloads[request.url.path])

    async with CoSensClient(token="ordinary", admin_token="administrator", _http_transport=httpx.MockTransport(server)) as client:
        artifact = await client.atoms.artifact(name="input.txt", content="hello")
        prepared = await client.atoms.prepare(objective="task", input_artifact_ids=[artifact.artifact_id], key="prepare-key")
        await client.atoms.get_run(prepared.run_id)
        await client.atoms.session_events("r1", [{"id": "e1", "kind": "user_prompt", "payload": {}}])
        await client.atoms.initial_inputs("r1", ["a1"])
        submitted = await client.atoms.submit("r1", data={"summary": "done"}, key="submit-key")
        await client.atoms.feedback("r1", data={"type": "correction", "comment": "fix"})
        await client.atoms.improve("r1", reason="fix", key="improve-key")
        await client.atoms.get_improvement("j1")
        await client.atoms.resume_improvement("j1")
        await client.admin.atoms.publish("j1", expected_revision=2, reason="verified")

    assert artifact.content_hash == "h1"
    assert submitted.accepted is None
    assert [r[1] for r in requests] == [
        "/api/v1/artifacts", "/api/v1/runs/prepare", "/api/v1/runs/r1", "/api/v1/runs/r1/session-events",
        "/api/v1/runs/r1/initial-inputs", "/api/v1/runs/r1/submit", "/api/v1/runs/r1/feedback", "/api/v1/runs/r1/improvements",
        "/api/v1/improvements/j1", "/api/v1/improvements/j1/resume",
        "/api/internal/admin/v1/improvements/j1/publish",
    ]
    assert requests[1][2]["idempotency-key"] == "prepare-key"
    assert requests[1][3] == {"objective": "task", "task_family": "general", "input_artifact_ids": ["a1"]}
    assert requests[3][3] == {"events": [{"id": "e1", "kind": "user_prompt", "payload": {}}]}
    assert requests[4][3] == {"artifact_ids": ["a1"]}
    assert requests[5][3] == {"data": {"summary": "done"}}
    assert requests[7][3] == {"data": {"reason": "fix"}}
    assert requests[10][2]["authorization"] == "Bearer administrator"
    assert all(r[2]["authorization"] == "Bearer ordinary" for r in requests[:10])


def test_public_constructor_has_no_service_base_url():
    with pytest.raises(TypeError):
        CoSensClient(token="ordinary", base_url="https://attacker.invalid")


@pytest.mark.asyncio
async def test_http_error_preserves_status_code_request_id_and_domain_code():
    def server(request):
        return httpx.Response(409, json={"error": {"code": "conflict", "message": "same key different body"}}, headers={"x-request-id": "req-1"})

    async with CoSensClient(token="ordinary", _http_transport=httpx.MockTransport(server)) as client:
        with pytest.raises(RSIHTTPError) as exc:
            await client.atoms.prepare(objective="task", key="same")
    assert exc.value.status_code == 409
    assert exc.value.code == "conflict"
    assert exc.value.request_id == "req-1"


@pytest.mark.asyncio
async def test_prepare_requires_stable_key_and_publish_requires_separate_admin_token():
    client = CoSensClient(token="ordinary", _http_transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    with pytest.raises(ValueError):
        await client.atoms.prepare(objective="task", key="")
    with pytest.raises(ValueError):
        await client.admin.atoms.publish("j1", expected_revision=None, reason="publish")
    await client.aclose()
