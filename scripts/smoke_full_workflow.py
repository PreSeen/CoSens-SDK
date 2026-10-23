"""Exercise the local Friday Memory + RSI + Streamable HTTP MCP path."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import StdioServerParameters, stdio_client


def read_local_env():
    for line in Path("var/local.env").read_text().splitlines():
        key, value = line.split("=", 1)
        os.environ[key] = value


async def main():
    read_local_env()
    rsi = os.environ.get("COSENS_RSI_BASE_URL", "http://127.0.0.1:18104").rstrip("/")
    memory = rsi + "/memory"
    mh = {"Authorization": "Bearer " + os.environ["COSENS_MEMORY_INTERNAL_TOKEN"]}
    rh = {"Authorization": "Bearer " + os.environ["COSENS_RSI_TOKEN"]}
    ah = {"Authorization": "Bearer " + os.environ["COSENS_RSI_ADMIN_TOKEN"]}
    run_marker = "CoSens RSI Friday Memory full flow " + uuid.uuid4().hex
    content = (
        f"{run_marker}. Project decision: deployment notes must preserve source links, "
        "and each workflow run stores a frozen memory snapshot before execution. "
        "The owner confirmed this as a durable project procedure for integration testing."
    )
    async with httpx.AsyncClient(timeout=20) as client:
        ingest = await client.post(memory + "/internal/v1/ingest", headers=mh, json={
            "content": content, "request_key": "smoke-" + uuid.uuid4().hex,
            "durable": True, "source_kind": "decision"})
        ingest.raise_for_status()
        receipt = ingest.json()
        for _ in range(30):
            job = await client.get(memory + "/internal/v1/jobs/" + receipt["job_id"], headers=mh)
            if job.status_code == 200 and job.json().get("state") in {"committed", "failed", "quarantined"}:
                break
            await asyncio.sleep(0.2)
        recall = await client.post(memory + "/internal/v1/recall", headers=mh,
            json={"query": run_marker, "token_budget": 1200})
        recall.raise_for_status()
        assert any(run_marker in item["text"] for item in recall.json()["items"]), recall.json()

        boot = await client.post(rsi + "/internal/admin/v1/recipes/bootstrap", headers=ah,
            json={"task_family": "full-flow-test", "reason": "smoke seed; explicitly unvalidated"})
        boot.raise_for_status()
        prepared = await client.post(rsi + "/v1/runs/prepare", headers={**rh, "Idempotency-Key": str(uuid.uuid4())}, json={
            "objective": run_marker, "task_family": "full-flow-test", "requires_evidence": True,
            "execution_mode": "assist", "deliverable": {"format": "markdown"}})
        prepared.raise_for_status()
        result = prepared.json()
        assert result["status"] == "ready", result
        assert result["recipe"]["status"] == "matched", result
        assert result["context"]["evidence"], result["context"]
        rid = result["run_id"]

        guidance = await client.post(f"{rsi}/v1/runs/{rid}/guidance", headers=rh,
            json={"data": {"observation": "source search succeeded"}})
        guidance.raise_for_status()
        assert guidance.json()["memory_snapshot_hash"] == result["context"]["hash"]
        checkpoint = await client.post(f"{rsi}/v1/runs/{rid}/checkpoint", headers=rh,
            json={"data": {"completed": ["memory retrieval"], "next": "produce report"}})
        checkpoint.raise_for_status()
        submitted = await client.post(f"{rsi}/v1/runs/{rid}/submit", headers=rh,
            json={"data": {"summary": "Completed integration smoke", "source_ref": receipt["source_id"]}})
        submitted.raise_for_status()
        assert submitted.json()["evaluation"] == "inconclusive"
        feedback = await client.post(f"{rsi}/v1/runs/{rid}/feedback", headers=rh,
            json={"data": {"type": "accepted", "note": "smoke flow feedback"}})
        feedback.raise_for_status()
        guided = await client.post(rsi + "/v1/runs/prepare", headers={**rh, "Idempotency-Key": str(uuid.uuid4())}, json={
            "objective": run_marker + " execute the frozen DAG", "task_family": "full-flow-test",
            "execution_mode": "guided", "requires_evidence": True})
        guided.raise_for_status()
        grid = guided.json()
        assert grid["status"] == "ready", grid
        brief = "Frozen task and source review completed for " + run_marker
        step = (await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/next", headers=rh)).json()
        assert step["node"]["id"] == "prepare"
        step_result = await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/{step['attempt_id']}/submit",
            headers=rh, json={"attempt_token": step["attempt_token"], "outputs": {"brief": brief}})
        step_result.raise_for_status()
        artifact_response = await client.post(rsi + "/v1/artifacts", headers=rh,
            json={"name": "guided-deliverable.md", "content": "Verified deliverable for " + run_marker})
        artifact_response.raise_for_status()
        artifact_ref = {"artifact_id": artifact_response.json()["artifact_id"],
            "content_hash": artifact_response.json()["content_hash"]}
        step = (await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/next", headers=rh)).json()
        assert step["node"]["id"] == "produce"
        step_result = await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/{step['attempt_id']}/submit",
            headers=rh, json={"attempt_token": step["attempt_token"], "outputs": {"files": [artifact_ref]}})
        step_result.raise_for_status()
        step = (await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/next", headers=rh)).json()
        assert step["node"]["id"] == "verify"
        step_result = await client.post(f"{rsi}/v1/runs/{grid['run_id']}/steps/{step['attempt_id']}/submit",
            headers=rh, json={"attempt_token": step["attempt_token"], "outputs": {"files": [artifact_ref]}})
        step_result.raise_for_status()
        complete = await client.post(f"{rsi}/v1/runs/{grid['run_id']}/complete", headers=rh)
        complete.raise_for_status()
        assert complete.json()["status"] == "completed"
        improvement = await client.post(f"{rsi}/v1/runs/{rid}/improvements", headers=rh,
            json={"data": {"reason": "explicit integration test request"}})
        improvement.raise_for_status()
        events = await client.get(f"{rsi}/v1/runs/{rid}/events", headers=rh)
        events.raise_for_status()
        assert [x["type"] for x in events.json()["items"]] == [
            "prepared", "checkpoint", "submitted", "feedback", "improvement_requested"]

    async with streamablehttp_client(rsi + "/mcp", headers=rh) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert "cosens_prepare_task" in names and "cosens_record_feedback" in names
            assert not any("memory" in name.lower() and name != "cosens_get_run" for name in names), names
            called = await session.call_tool("cosens_prepare_task", {"objective": run_marker + " MCP check",
                "task_family": "full-flow-test", "execution_mode": "assist", "requires_evidence": True})
            assert not called.isError, called
            payload = json.loads(called.content[0].text)
            assert payload["status"] == "ready", payload
            assert payload["context"]["evidence"], payload["context"]

    async with stdio_client(StdioServerParameters(command=str(Path(__file__).resolve().parent / "cosens-rsi-mcp"),
                                                   args=[], cwd=str(Path(__file__).resolve().parents[1]))) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "cosens_prepare_task" in {tool.name for tool in tools.tools}

    print(json.dumps({"memory_ingest": "ok", "memory_recall": "ok", "rsi_recipe": "ok",
        "frozen_snapshot": "ok", "checkpoint_submit_feedback": "ok", "mcp_list_call": "ok",
        "codex_stdio_mcp": "ok",
        "guided_hierarchical_dag": "ok", "run_id": rid, "guided_run_id": grid["run_id"],
        "mcp_run_id": payload["run_id"], "improvement": improvement.json()}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
