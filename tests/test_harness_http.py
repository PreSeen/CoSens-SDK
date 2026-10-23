"""Real configured API worker, command actor, fixed artifact tests and MCP read."""
import asyncio
import json
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from test_experience_http import local_server as _server_fixture

local_server = _server_fixture


@pytest.mark.parametrize("local_server", ["harness"], indirect=True)
def test_configured_harness_roundtrip(local_server):
    url, env = local_server
    public = {"Authorization": "Bearer " + env["COSENS_RSI_TOKEN"]}
    admin = {"Authorization": "Bearer " + env["COSENS_RSI_ADMIN_TOKEN"]}
    with httpx.Client(base_url=url, headers=public, timeout=10) as client:
        health = client.get("/health/ready").json()
        assert health["harness_improvement"]["configured_families"] == ["files"]
        seed = client.post("/internal/admin/v1/recipes/bootstrap", headers=admin,
                           json={"task_family": "files", "reason": "isolated real worker test"})
        seed.raise_for_status()
        run = client.post("/v1/runs/prepare", headers={**public, "Idempotency-Key": "harness-run"},
                          json={"objective": "Calculate a total", "task_family": "files"}).json()
        request = client.post(f"/v1/runs/{run['run_id']}/improvements", json={"data": {"reason": "avoid repeated probes"}})
        request.raise_for_status()
        job = request.json()
        assert job["status"] == "queued"
        for _ in range(300):
            response = client.get(f"/v1/improvements/{job['job_id']}")
            response.raise_for_status()
            report = response.json()
            if report["status"] not in {"queued", "running"}:
                break
            time.sleep(.05)
        assert report["status"] == "validated", report
        assert report["report"]["release_decision"]["acceptance_path"] == "exploration"
        assert client.get("/v1/recipes/active", params={"task_family": "files"}).json()["revision"] == 1
        path = f"/internal/admin/v1/improvements/{job['job_id']}/publish"
        body = {"expected_revision": 1, "reason": "independent artifact checks passed"}
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, headers=admin, json={**body, "expected_revision": 2}).status_code == 422
        published = client.post(path, headers=admin, json=body)
        published.raise_for_status()
        assert published.json()["revision"] == 2

    async def read_mcp():
        async with httpx.AsyncClient(headers=public) as http:
            async with streamable_http_client(url + "/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    assert "cosens_get_improvement" in tools
                    assert not any("publish" in t for t in tools)
                    result = await session.call_tool("cosens_get_improvement", {"job_id": job["job_id"]})
                    assert not result.isError
                    assert json.loads(result.content[0].text)["status"] == "published"

    asyncio.run(read_mcp())
