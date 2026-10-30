"""Exercise the IDEA CN smoke entry against a contract-shaped local server."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from cosens import CoSensClient


SCRIPT = Path(__file__).resolve().parents[1] / "examples/04_task_workflow_cn_smoke.py"
spec = importlib.util.spec_from_file_location("cn_workflow_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.asyncio
async def test_cn_smoke_stops_without_writes_when_workflow_is_not_deployed(tmp_path):
    requests = []

    def server(request):
        requests.append(request.method + " " + request.url.path)
        if request.url.path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"info": {"version": "1.0.0"}, "paths": {}})

    async with CoSensClient(api_key="test", _http_transport=httpx.MockTransport(server)) as client:
        report = {"status": "running"}
        code = await smoke.validate(client, report, tmp_path / "report.json")
    assert code == 2
    assert report["status"] == "workflow_not_deployed"
    assert all(request.startswith("GET ") for request in requests)


@pytest.mark.asyncio
async def test_cn_smoke_submits_actual_output_and_replays_exact_result(tmp_path):
    requests = []
    submissions = []
    output = "COSENS SDK CN WORKFLOW SMOKE TEST"

    def server(request):
        path = request.url.path
        requests.append((request.method, path, request.headers.get("x-auth-type"), request.headers.get("authorization")))
        body = json.loads(request.content) if request.content else None
        if path.endswith("/health/ready"):
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/openapi.json"):
            return httpx.Response(200, json={"info": {"version": "2.0.0"}, "paths": {"/v1/tasks": {}}})
        if path.endswith("/v1/artifacts"):
            artifact_id = "a1" if body["name"].endswith("input.txt") else "a2"
            return httpx.Response(200, json={"artifact_id": artifact_id, "content_hash": "hash-" + artifact_id})
        if path.endswith("/v1/tasks"):
            assert body["source"] == "new_task" and body["artifacts"] == ["a1"]
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if path.endswith("/v1/tasks/t1/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "frozen-hash", "request": {"kind": "original"}})
        if path.endswith("/v1/executions/e1/result"):
            submissions.append(body)
            return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        if path.endswith("/v1/executions/e1"):
            return httpx.Response(200, json={"execution_id": "e1", "request_sha256": "frozen-hash"})
        if path.endswith("/v1/revisions/r1"):
            return httpx.Response(200, json={"revision_id": "r1"})
        if path.endswith("/v1/artifacts/a2"):
            return httpx.Response(200, json={"artifact_id": "a2", "content_hash": "hash-a2", "content": output})
        raise AssertionError(path)

    async with CoSensClient(api_key="test", _http_transport=httpx.MockTransport(server)) as client:
        report = {"status": "running"}
        code = await smoke.validate(client, report, tmp_path / "report.json")
    assert code == 0 and report["status"] == "passed"
    assert submissions[0] == submissions[1] == report["result_body"]
    assert submissions[0]["events"][0]["kind"] == "tool_result"
    assert submissions[0]["artifacts"] == [{"artifact_id": "a2", "role": "submitted_output"}]
    assert all(auth_type == "api_key" and authorization == "Bearer test" for _, _, auth_type, authorization in requests)
    assert json.loads((tmp_path / "report.json").read_text())["revision_id"] == "r1"
