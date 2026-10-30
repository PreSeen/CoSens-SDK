"""Public v3 Memory and guided Run contract at the authenticated gateway boundary."""

import json

import httpx
import pytest

from cosens import CoSensClient


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_v3_memory_and_guided_run_requests(asynchronous):
    seen = []

    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms
        calls = [
            atoms.memory_ingest(content="updated", request_key="source-2", source_id="s1", expected_revision=2,
                                source_kind="message", durable=False),
            atoms.get_memory_items(limit=500),
            atoms.next_step("run-1"),
            atoms.submit_step("run-1", "attempt-1", attempt_token="opaque-token", outputs={"report": "a1"},
                              decision_id="d1", actual_action_id="check"),
            atoms.complete_guided("run-1"),
            atoms.get_events("run-1"),
        ] if not asynchronous else []
        if asynchronous:
            calls = [
                await atoms.memory_ingest(content="updated", request_key="source-2", source_id="s1", expected_revision=2,
                                          source_kind="message", durable=False),
                await atoms.get_memory_items(limit=500),
                await atoms.next_step("run-1"),
                await atoms.submit_step("run-1", "attempt-1", attempt_token="opaque-token", outputs={"report": "a1"},
                                        decision_id="d1", actual_action_id="check"),
                await atoms.complete_guided("run-1"),
                await atoms.get_events("run-1"),
            ]
        assert len(calls) == 6

    assert [(r.method, r.url.path, r.url.query.decode()) for r in seen] == [
        ("POST", "/api/v1/memory/ingest", ""),
        ("GET", "/api/v1/memory/items", "limit=500"),
        ("POST", "/api/v1/runs/run-1/steps/next", ""),
        ("POST", "/api/v1/runs/run-1/steps/attempt-1/submit", ""),
        ("POST", "/api/v1/runs/run-1/complete", ""),
        ("GET", "/api/v1/runs/run-1/events", ""),
    ]
    assert json.loads(seen[0].content) == {"content": "updated", "request_key": "source-2", "source_id": "s1",
                                           "expected_revision": 2, "source_kind": "message", "durable": False}
    assert json.loads(seen[3].content) == {"attempt_token": "opaque-token", "outputs": {"report": "a1"},
                                           "decision_id": "d1", "actual_action_id": "check"}
    assert all(r.headers["authorization"] == "Bearer gateway-key" and r.headers["x-auth-type"] == "api_key" for r in seen)
    assert all("x-user-id" not in r.headers for r in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_v3_memory_limits_fail_before_request(asynchronous):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={})
    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms
        for limit in (0, 501, 1.5, True):
            with pytest.raises((ValueError, TypeError)):
                value = atoms.get_memory_items(limit=limit)
                if asynchronous: await value
        for budget in (0, 8001, 2.5, True):
            with pytest.raises((ValueError, TypeError)):
                value = atoms.memory_recall(query="q", token_budget=budget)
                if asynchronous: await value
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_prepare_rejects_unavailable_managed_mode(asynchronous):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"run_id": "r1"})
    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms
        with pytest.raises(ValueError, match="execution_mode"):
            value = atoms.prepare(objective="work", key="k1", execution_mode="managed")
            if asynchronous: await value
        value = atoms.prepare(objective="work", key="k2", execution_mode="guided")
        if asynchronous: await value
    assert len(seen) == 1
    assert json.loads(seen[0].content)["execution_mode"] == "guided"
