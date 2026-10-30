import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIApprovalUnavailable, RSIApprovalRequired


@pytest.mark.asyncio
async def test_start_prepare_returns_frozen_host_request_and_reuses_keys():
    seen = []

    def server(request):
        seen.append((request.url.path, request.headers.get("idempotency-key"), json.loads(request.content) if request.content else None))
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        return httpx.Response(200, json={"execution_id": "e1", "request_sha256": "frozen-hash", "request": {"kind": "original"}, "status": "awaiting_execution"})

    async with CoSensClient(api_key="key", _http_transport=httpx.MockTransport(server)) as client:
        snapshot = {"task": {"source": "new_task", "objective": "work", "artifacts": []}}
        first = await client.start(snapshot, request_key="business-turn-1")
        second = await client.start(snapshot, request_key="business-turn-1")
    assert first.execution_request == {"kind": "original"}
    assert first.request_sha256 == "frozen-hash"
    assert second.execution_id == "e1"
    assert seen[0][0] == "/api/v1/tasks"
    assert seen[1][0] == "/api/v1/tasks/t1/executions"
    assert seen[0][1] == seen[2][1] and seen[1][1] == seen[3][1]
    assert seen[0][1] != seen[1][1]
    assert seen[0][2] == {"source": "new_task", "objective": "work", "artifacts": []}


@pytest.mark.asyncio
async def test_start_needs_context_and_disable_and_approval_do_not_execute_host():
    seen = []

    def server(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"task_id": "t1", "status": "needs_context", "missing": ["objective"]})

    async with CoSensClient(api_key="key", _http_transport=httpx.MockTransport(server)) as client:
        disabled = await client.start({}, mode="disable")
        assert disabled.status == "disabled"
        with pytest.raises(RSIApprovalUnavailable):
            await client.start({"task": {"source": "external_artifact", "artifacts": ["a1"]}}, mode="approval", request_key="k1")
        with pytest.raises(RSIApprovalRequired):
            await client.start({"task": {"source": "external_artifact", "artifacts": ["a1"]}}, mode="approval", request_key="k1", authorize=lambda *_: False)
        response = await client.start({"task": {"source": "external_artifact", "artifacts": ["a1"]}}, request_key="k1")
    assert response.status == "needs_context"
    assert response.get("execution_id") is None
    assert seen == ["/api/v1/tasks"]


@pytest.mark.asyncio
async def test_start_rejects_ready_task_without_id_before_execution():
    calls = []

    def server(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": "ready"})

    async with CoSensClient(api_key="key", _http_transport=httpx.MockTransport(server)) as client:
        from cosens.errors import RSIRecoveryRequired
        with pytest.raises(RSIRecoveryRequired, match="task_id"):
            await client.start({"task": {"source": "new_task", "objective": "work"}}, request_key="k1")
    assert calls == ["/api/v1/tasks"]
