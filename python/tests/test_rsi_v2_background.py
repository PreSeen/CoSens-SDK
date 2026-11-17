"""Actual detached subprocesses + LangChain tool loop + LangGraph + SQLite.

The OpenAI-compatible endpoint below is a protocol fixture, NOT a live LLM.
"""

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("langchain_openai")
pytest.importorskip("langgraph.checkpoint.sqlite")

from cosens.rsi import RSIClient


@pytest.fixture
def gateway():
    class Gateway(ThreadingHTTPServer):
        daemon_threads = True
        active = 0
        peak = 0
        requests = 0
        structured_budget = 0
        bad_output = "malformed"
        lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            server = self.server
            with server.lock:
                server.active += 1
                server.peak = max(server.peak, server.active)
                server.requests += 1
            try:
                time.sleep(0.03)
                messages = request["messages"]
                system = messages[0]["content"]
                payload = json.loads(
                    next(m["content"] for m in messages if m["role"] == "user")
                )
                tools = {t["function"]["name"] for t in request.get("tools", [])}
                called = any(m["role"] == "tool" for m in messages)
                if not called:
                    if "recipe test executor" in system:
                        name, arguments = (
                            "write_output",
                            {"path": "answer.txt", "content": "42"},
                        )
                    else:
                        name, arguments = (
                            "read_evidence",
                            {"locator": payload["evidence_catalog"][0]["locator"]},
                        )
                elif "Analysis" in tools:
                    locator = payload["evidence_catalog"][0]["locator"]
                    name, arguments = (
                        "Analysis",
                        {
                            "sufficient_information": True,
                            "hypothesis": None,
                            "questions": [],
                            "rubrics": {
                                "criteria": [
                                    {
                                        "id": "correct",
                                        "description": "answer.txt must contain 42",
                                        "source": "task",
                                        "evidence": [
                                            {
                                                "material_id": locator.split("/")[0],
                                                "locator": locator,
                                            }
                                        ],
                                    }
                                ]
                            },
                        },
                    )
                elif "RecipeContent" in tools:
                    name, arguments = (
                        "RecipeContent",
                        {
                            "title": "Write 42",
                            "applicability": "Fixed answer task",
                            "instructions": "Write 42 into answer.txt using write_output",
                        },
                    )
                elif "Assessment" in tools:
                    name, arguments = (
                        "Assessment",
                        {
                            "verifiable": True,
                            "scores": [
                                {
                                    "criterion_id": "correct",
                                    "score": 1,
                                    "evidence": "Read test/answer.txt: 42",
                                }
                            ],
                            "reason": "Output checked",
                        },
                    )
                else:
                    name, arguments = None, None
                if called and name == "Analysis" and server.structured_budget:
                    server.structured_budget -= 1
                    if server.bad_output == "prose":
                        name, arguments = None, None
                    elif server.bad_output == "bad_material":
                        reference = arguments["rubrics"]["criteria"][0]["evidence"][0]
                        reference["material_id"] = reference["locator"]
                    else:
                        arguments = '{"sufficient_information": true, "hypothesis": invalid-json}'
                message = {
                    "role": "assistant",
                    "content": None if name else "Deliverable written",
                }
                if name:
                    message["tool_calls"] = [
                        {
                            "id": "call_" + str(server.requests),
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments
                                if isinstance(arguments, str)
                                else json.dumps(arguments),
                            },
                        }
                    ]
                response = {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "local-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if name else "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                }
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with server.lock:
                    server.active -= 1

    server = Gateway(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_multiple_hook_processes_survive_exit_and_share_limits(
    tmp_path, gateway, monkeypatch
):
    monkeypatch.setenv("RSI_MODEL_API_KEY", "fixture-not-a-live-secret")
    monkeypatch.delenv("COSENS_API_KEY", raising=False)
    root = tmp_path / "local-rsi"
    config = {
        "model": {
            "model": "local-test",
            "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        },
        "max_executions": 2,
        "max_model_requests": 1,
    }
    client = RSIClient(data_dir=root, config=config)
    source = tmp_path / "context.md"
    source.write_text(
        "User confirmed artifact. Learn how to write the fixed answer 42 into answer.txt."
    )
    material = client.material_create(manifest={"context_path": str(source)})
    # Each short-lived caller models an independent host session's Hook.
    script = """
import json, sys
from cosens.rsi import RSIClient
c = RSIClient(data_dir=sys.argv[1])
run = c.execution_create(material_ids=[sys.argv[2]], source_session_id=sys.argv[3],
    idempotency_key='hook-' + sys.argv[3], policy={
        'artifact_confirmed': True, 'confirmation_ref': 'host-confirmation', 'auto_activate': True,
        'test_task': {'instruction': 'Write 42 into answer.txt', 'checks': [
            {'kind': 'text_equals', 'path': 'answer.txt', 'value': '42'}]}})
print(json.dumps({'id': run['id']}))
"""
    callers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(root),
                material["id"],
                f"session-{index}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for index in range(4)
    ]
    ids = []
    try:
        for caller in callers:
            stdout, stderr = caller.communicate(timeout=20)
            assert caller.returncode == 0, stderr
            ids.append(json.loads(stdout)["id"])
        assert len(set(ids)) == 4
        peak_running = 0
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            runs = [client.execution_get(i) for i in ids]
            peak_running = max(
                peak_running, sum(r["status"] == "running" for r in runs)
            )
            if all(r["status"] in {"completed", "failed", "cancelled"} for r in runs):
                break
            time.sleep(0.1)
        assert all(r["status"] == "completed" for r in runs), runs
        assert all(r["result"]["activated"] for r in runs), runs
        assert peak_running == 2
        assert gateway.peak == 1  # Separate model budget across all processes.
        assert gateway.requests >= 32  # 4 stages, tool call + result, 4 runs.
        for run in runs:
            test = run["test_results"][0]
            assert test["status"] == "passed"
            from pathlib import Path

            trace = json.loads(Path(test["trace_path"]).read_text())
            assert any(m["type"] == "tool" for m in trace["messages"])
    finally:
        for caller in callers:
            if caller.poll() is None:
                caller.kill()
                caller.wait()
        for execution_id in ids:
            if client.execution_get(execution_id)["status"] not in {
                "completed",
                "failed",
                "cancelled",
            }:
                client.execution_cancel(execution_id)


@pytest.mark.parametrize(
    "bad_output,budget",
    [("malformed", 1), ("prose", 1), ("malformed", 10), ("bad_material", 1)],
)
def test_structured_output_correction_is_bounded(
    tmp_path, gateway, monkeypatch, bad_output, budget
):
    monkeypatch.setenv("RSI_MODEL_API_KEY", "fixture-not-a-live-secret")
    gateway.bad_output = bad_output
    gateway.structured_budget = budget
    client = RSIClient(
        data_dir=tmp_path / "rsi",
        config={
            "model": {
                "model": "local-test",
                "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
            }
        },
        auto_start=False,
    )
    context = tmp_path / "context.md"
    context.write_text("Write 42 into answer.txt")
    material = client.material_create(manifest={"context_path": str(context)})
    execution = client.execution_create(
        material_ids=[material["id"]],
        policy={
            "artifact_confirmed": True,
            "confirmation_ref": "fixture",
            "test_task": {"instruction": "Write 42 into answer.txt"},
        },
    )
    client.run_pending()
    result = client.execution_get(execution["id"])
    if budget > 3:
        assert result["status"] == "failed"
        assert result["error"]["code"] == "invalid_model_output"
        assert client.recipe_list() == []
        assert gateway.requests == 4  # one read + three malformed output attempts
    else:
        assert result["status"] == "completed", result
        assert result["result"]["test_status"] == "passed"
        traces = list(client.data_dir.glob("executions/*/input-*/analysis.json"))
        trace = json.loads(traces[0].read_text())
        assert trace["structured_output_attempts"] == (
            1 if bad_output == "bad_material" else 2
        )
