"""Exercise the real mounted Memory app, dispatcher and both MCP transports."""
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client


@pytest.fixture
def local_server(tmp_path, request):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    env = dict(os.environ)
    env.pop("COSENS_RSI_HARNESS_BACKEND", None)
    env.pop("COSENS_RSI_GENERIC_RUNTIME", None)
    env.pop("COSENS_RSI_GENERIC_CONFIG", None)
    if getattr(request, "param", None) == "harness":
        import inspect
        from test_harness import FileDriver, cases, event
        actor = tmp_path / "actor.py"
        actor.write_text("import json,sys\nfrom pathlib import Path\n"
                         "from cosens_rsi.algorithms.hierarchical import fingerprint\n" +
                         inspect.getsource(event) + "\n" + inspect.getsource(FileDriver) +
                         '\nprint(json.dumps(FileDriver(Path.cwd()).execute(json.load(sys.stdin)["instruction"], timeout=10)))\n')
        config = tmp_path / "harness.json"
        config.write_text(json.dumps({"files": {"agent_argv": [sys.executable, str(actor)],
            "driver_version": "file-command-v1", "cases": cases(),
            "learning_policy": {"max_rounds": 1, "max_candidates": 2}}}))
        env.update(COSENS_RSI_HARNESS_BACKEND="scripts.harness_backend_example:build_backends",
                   COSENS_RSI_HARNESS_CONFIG=str(config))
    if getattr(request, "param", None) == "generic":
        import inspect
        from test_generic_improvement import FileAgent, proposer
        module = tmp_path / "generic_fixture.py"
        module.write_text('import json\nfrom copy import deepcopy\nfrom typing import ClassVar\n'
            'from cosens_rsi.runtime import RuntimeProfile\n'
            'from cosens_rsi.algorithms.hierarchical_learning import LearningPolicy\n' +
            inspect.getsource(FileAgent) + '\n' + inspect.getsource(proposer) + '\nproposer.calls=[]\n'
            'def build_runtime():\n'
            '    return RuntimeProfile("http-file-agent-v1",FileAgent,isolated=True,proposer=proposer,\n'
            '        policy=LearningPolicy(max_rounds=1),train_rollouts=1)\n')
        env.update(COSENS_RSI_GENERIC_RUNTIME="generic_fixture:build_runtime",
                   PYTHONPATH=str(tmp_path) + os.pathsep + env.get("PYTHONPATH", ""))
    env.update(COSENS_RSI_DB=str(tmp_path / "rsi.sqlite"), MEMORY_DB=str(tmp_path / "memory.sqlite"),
               COSENS_MEMORY_URL=url + "/memory", COSENS_MEMORY_PROJECT="default",
               COSENS_MEMORY_INTERNAL_TOKEN=secrets.token_urlsafe(24), COSENS_RSI_TOKEN=secrets.token_urlsafe(24),
               COSENS_RSI_ADMIN_TOKEN=secrets.token_urlsafe(24), COSENS_RSI_RUNTIME_TRIGGER="manual",
               COSENS_RSI_IMPROVEMENT_TRIGGER="manual", COSENS_RSI_EXPERIENCE_MODE="advisory")
    log = tmp_path / "server.log"
    with log.open("w") as output:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "cosens_rsi.api:app", "--fd", str(listener.fileno())],
                                   env=env, pass_fds=(listener.fileno(),), stdout=output, stderr=output,
                                   cwd=Path(__file__).resolve().parents[1])
    listener.close()
    try:
        for _ in range(150):
            if process.poll() is not None:
                pytest.fail(log.read_text())
            try:
                if httpx.get(url + "/health/live", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("server did not start: " + log.read_text())
        yield url, env
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_experience_loop_http_and_both_mcp_transports(local_server):
    url, env = local_server
    public = {"Authorization": "Bearer " + env["COSENS_RSI_TOKEN"]}
    admin = {"Authorization": "Bearer " + env["COSENS_RSI_ADMIN_TOKEN"]}
    internal = {"Authorization": "Bearer " + env["COSENS_MEMORY_INTERNAL_TOKEN"]}
    actions = [{"id": "retry", "description": "Retry the request", "confidence": 0.8},
               {"id": "inspect", "description": "Inspect the response schema", "confidence": 0.6}]

    with httpx.Client(base_url=url, headers=public, timeout=10) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/memory/internal/v1/experiences/watermark").status_code == 401

        def prepare(label):
            response = client.post("/v1/runs/prepare", headers={**public, "Idempotency-Key": label},
                json={"objective": "Diagnose response schema validation failure " + label, "task_family": "http-test"})
            response.raise_for_status()
            return response.json()

        def wait_revision(run, decision, revision):
            for _ in range(100):
                response = client.get(f"/v1/runs/{run}/decisions/{decision}")
                response.raise_for_status()
                data = response.json()
                if data["experience"]["revision"] >= revision:
                    return data
                assert not any(w["state"] == "failed" for w in data["writes"]), data
                time.sleep(0.05)
            pytest.fail("experience dispatcher did not deliver")

        # Experience comparisons use a constant Recipe compatibility scope.
        client.post("/internal/admin/v1/recipes/bootstrap", headers=admin,
                    json={"task_family": "http-test", "reason": "isolated integration baseline"}).raise_for_status()
        old_run = prepare("old")
        for i, (actual, reward) in enumerate((("retry", -1), ("inspect", 1))):
            run = prepare(f"case-{i}")["run_id"]
            response = client.post(f"/v1/runs/{run}/guidance", json={"data": {
                "observation": "response schema validation failure", "candidates": actions}})
            response.raise_for_status()
            decision = response.json()["decision_id"]
            response = client.post(f"/v1/runs/{run}/checkpoint", json={"data": {
                "decision_id": decision, "actual_action_id": actual, "next_state": {"observed": "request executed"},
                "reward": 1, "verified": True}})
            response.raise_for_status()
            state = wait_revision(run, decision, 1)
            experience_id = state["experience"]["experience_id"]
            stored = client.get("/memory/internal/v1/experiences/" + experience_id, headers=internal)
            stored.raise_for_status()
            assert stored.json()["record"]["outcome"]["status"] == "pending"
            artifact = client.post("/v1/artifacts", json={"name": "evaluation.json", "content": '{"checked":true}'}).json()
            evaluation = {"evaluation_id": f"eval-{i}", "reward_spec": "verified-decision-return-v1",
                "status": "verified", "value": reward, "evidence_artifact_id": artifact["artifact_id"],
                "evidence_content_hash": artifact["content_hash"]}
            path = f"/internal/admin/v1/runs/{run}/decisions/{decision}/evaluations"
            assert client.post(path, json=evaluation).status_code == 401
            confirmed = client.post(path, headers=admin, json=evaluation)
            confirmed.raise_for_status()
            assert client.post(path, headers=admin, json=evaluation).json() == confirmed.json()
            wait_revision(run, decision, 2)
            stored = client.get("/memory/internal/v1/experiences/" + experience_id, headers=internal).json()
            assert stored["record"]["actual_action_id"] == actual
            assert stored["record"]["outcome"]["value"] == reward

        old = client.post(f"/v1/runs/{old_run['run_id']}/guidance", json={"data": {
            "observation": "response schema validation failure", "candidates": actions}}).json()
        assert old["decision"]["cold_start"]
        assert client.get("/internal/admin/v1/memory-outbox", headers=admin).json() == {"done": 4}

        guided = client.post("/v1/runs/prepare", headers={**public, "Idempotency-Key": "guided"}, json={
            "objective": "Execute the frozen guided workflow", "task_family": "http-test", "execution_mode": "guided"})
        guided.raise_for_status()
        gid = guided.json()["run_id"]
        assert client.post(f"/v1/runs/{gid}/submit", json={"data": {"summary": "skip DAG"}}).status_code == 422
        artifact = client.post("/v1/artifacts", json={"name": "deliverable.md", "content": "Saved guided deliverable"}).json()
        artifact_ref = {k: artifact[k] for k in ("artifact_id", "content_hash")}
        for node in ("prepare", "produce", "verify"):
            step_response = client.post(f"/v1/runs/{gid}/steps/next")
            step_response.raise_for_status()
            step = step_response.json()
            assert step["node"]["id"] == node
            guidance = client.post(f"/v1/runs/{gid}/guidance", json={"data": {
                "node_id": node, "observation": "response schema validation failure", "candidates": actions}})
            guidance.raise_for_status()
            decision_id = guidance.json()["decision_id"]
            outputs = {"brief": "Frozen goal and requirements"} if node == "prepare" else {"files": [artifact_ref]}
            submitted = client.post(f"/v1/runs/{gid}/steps/{step['attempt_id']}/submit", json={
                "attempt_token": step["attempt_token"], "outputs": outputs,
                "decision_id": decision_id, "actual_action_id": "inspect"})
            submitted.raise_for_status()
            state = wait_revision(gid, decision_id, 1)
            saved = client.get("/memory/internal/v1/experiences/" + state["experience"]["experience_id"], headers=internal).json()
            assert saved["record"]["phase"] == node
            assert saved["record"]["outcome"]["status"] == "pending"
        complete = client.post(f"/v1/runs/{gid}/complete")
        complete.raise_for_status()
        assert complete.json()["status"] == "completed" and complete.json()["evaluation"] == "inconclusive"

    async def check_session(session):
        await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        assert "cosens_get_decision" in names
        assert not any("experience" in n or "evaluate" in n or "memory" in n for n in names)

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            assert not result.isError, result
            return json.loads(result.content[0].text)

        run = await call("cosens_prepare_task", {"objective": "Diagnose response schema validation failure through MCP",
                                               "task_family": "http-test"})
        decision = await call("cosens_request_guidance", {"run_id": run["run_id"],
            "observation": "response schema validation failure", "candidates": actions})
        assert decision["decision"]["recommended_action_id"] == "inspect"
        assert not decision["decision"]["cold_start"]
        await call("cosens_checkpoint", {"run_id": run["run_id"], "checkpoint": {
            "decision_id": decision["decision_id"], "actual_action_id": "inspect", "next_state": {"observed": "done"}}})
        saved = await call("cosens_get_decision", {"run_id": run["run_id"], "decision_id": decision["decision_id"]})
        assert saved["actual"]["actual_action_id"] == "inspect"

    async def check_transports():
        async with streamablehttp_client(url + "/mcp", headers=public) as (read, write, _):
            async with ClientSession(read, write) as session:
                await check_session(session)
        # The same API process dispatches writes produced by the stdio RSI sidecar.
        params = StdioServerParameters(command=sys.executable,
            args=["-c", "from cosens_rsi.mcp_stdio import run; run()"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await check_session(session)

    asyncio.run(check_transports())
