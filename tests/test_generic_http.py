"""Real HTTP worker and MCP transport for the generic preparation path."""

import asyncio
import json
import time

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_experience_http import local_server as _server_fixture

local_server = _server_fixture


@pytest.mark.parametrize("local_server", ["generic"], indirect=True)
def test_generic_worker_without_family_factory_and_extended_mcp_contract(local_server):
    url, env = local_server
    public = {"Authorization": "Bearer " + env["COSENS_RSI_TOKEN"]}
    admin = {"Authorization": "Bearer " + env["COSENS_RSI_ADMIN_TOKEN"]}
    with httpx.Client(base_url=url, headers=public, timeout=15) as client:
        health = client.get("/health/ready").json()
        assert health["harness_improvement"]["configured_families"] == []
        assert health["harness_improvement"]["generic_runtime"] == "http-file-agent-v1"
        runs = []
        for index in range(6):
            a = client.post(
                "/v1/artifacts",
                json={
                    "name": "input.json",
                    "content": json.dumps({"values": [index + 1, index + 2]}),
                },
            )
            a.raise_for_status()
            response = client.post(
                "/v1/runs/prepare",
                headers={**public, "Idempotency-Key": "generic-" + str(index)},
                json={
                    "objective": f"Case {index} sum every value from the attached original input.",
                    "task_family": "brand-new-http-family",
                    "input_artifact_ids": [a.json()["artifact_id"]],
                    "deliverable": {"path": "answer.json", "requires_files": True},
                    "acceptance": {
                        "checks": [
                            {
                                "kind": "exists",
                                "path": "answer.json",
                                "critical": False,
                            },
                            {
                                "kind": "json_equal",
                                "path": "answer.json",
                                "value": {"total": index * 2 + 3},
                                "critical": False,
                            },
                        ]
                    },
                },
            )
            response.raise_for_status()
            runs.append(response.json())
        run = runs[0]["run_id"]
        capture = {
            "events": [
                {
                    "id": "session:byte:100",
                    "kind": "tool_result",
                    "payload": {"stdout": "partial"},
                }
            ]
        }
        path = f"/v1/runs/{run}/session-events"
        assert (
            client.post(
                path, headers={"Authorization": "Bearer invalid"}, json=capture
            ).status_code
            == 401
        )
        ack = client.post(path, json=capture)
        ack.raise_for_status()
        assert client.post(path, json=capture).json() == ack.json()
        assert (
            client.post(
                path,
                json={
                    "events": [{**capture["events"][0], "payload": {"changed": True}}]
                },
            ).status_code
            == 422
        )
        response = client.post(
            f"/v1/runs/{run}/improvements",
            json={"data": {"reason": "Improve the incomplete aggregation"}},
        )
        response.raise_for_status()
        job = response.json()
        for _ in range(200):
            result = client.get(f"/v1/improvements/{job['job_id']}").json()
            if result["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert result["status"] == "validated", result
        release_path = f"/internal/admin/v1/improvements/{job['job_id']}/publish"
        assert (
            client.post(
                release_path, json={"expected_revision": None, "reason": "verified"}
            ).status_code
            == 401
        )
        published = client.post(
            release_path,
            headers=admin,
            json={"expected_revision": None, "reason": "verified"},
        )
        published.raise_for_status()
        assert published.json()["revision"] == 1

    async def mcp_contract():
        async with (
            httpx.AsyncClient(headers=public) as http,
            streamable_http_client(url + "/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = {t.name: t for t in (await session.list_tools()).tools}
            assert {
                "cosens_capture_session_events",
                "cosens_resume_improvement",
                "cosens_supply_initial_inputs",
            } <= tools.keys()
            prepare = tools["cosens_prepare_task"].inputSchema["properties"]
            assert {
                "input_artifact_ids",
                "acceptance",
                "deliverable",
                "request_key",
            } <= prepare.keys()
            assert not any("publish" in t for t in tools)
            captured = await session.call_tool(
                "cosens_capture_session_events",
                {"run_id": run, "events": capture["events"]},
            )
            assert not captured.isError
            assert json.loads(captured.content[0].text)["durable_ack"] == [
                "session:byte:100"
            ]
            readback = await session.call_tool(
                "cosens_get_improvement", {"job_id": job["job_id"]}
            )
            assert json.loads(readback.content[0].text)["status"] == "published"

    asyncio.run(mcp_contract())
