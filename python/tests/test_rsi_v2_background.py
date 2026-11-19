"""Actual detached subprocesses + LangChain tool loop + LangGraph + SQLite.

The OpenAI-compatible endpoint below is a protocol fixture, NOT a live LLM.
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from cosens.rsi._queue_client import RSIClient


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.delenv("COSENS_RSI_WEB_SEARCH_API_KEY", raising=False)

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
                if "web_search" in tools:
                    assert "RecipeContent" in tools
                called = any(m["role"] == "tool" for m in messages)
                catalog = payload["evidence_catalog"]
                feedback_source = next(
                    (f for f in catalog if f["role"] == "feedback"), None
                )
                feedback = (
                    [
                        {
                            "kind": "correction",
                            "rule": "Write only the corrected answer",
                            "evidence_refs": [
                                {
                                    k: feedback_source[k]
                                    for k in ("material_id", "locator")
                                }
                            ],
                        }
                    ]
                    if feedback_source
                    else []
                )
                if not called:
                    if "recipe test executor" in system:
                        name, arguments = (
                            "write_output",
                            {"path": "answer.txt", "content": "42"},
                        )
                    else:
                        name, arguments = (
                            "read_evidence",
                            {
                                "locator": (
                                    feedback_source
                                    if "FeedbackExtraction" in tools and feedback_source
                                    else catalog[0]
                                )["locator"]
                            },
                        )
                elif "FeedbackExtraction" in tools:
                    assert all(f["role"] in {"context", "feedback"} for f in catalog)
                    name, arguments = "FeedbackExtraction", {"items": feedback}
                elif "Analysis" in tools:
                    assert payload["context"]["user_feedback"] == feedback
                    locator = catalog[0]["locator"]
                    name, arguments = (
                        "Analysis",
                        {
                            "recipe_name": "Write checked answers",
                            "sufficient_information": True,
                            "hypothesis": None,
                            "questions": [],
                            "rubrics": {
                                "criteria": [
                                    {
                                        "id": "correct",
                                        "description": "answer.txt must contain 42",
                                        "source": "user_feedback"
                                        if feedback
                                        else "task",
                                        "evidence": feedback[0]["evidence_refs"]
                                        if feedback
                                        else [
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
                elif "web_search" in tools and not any(
                    m["role"] == "tool"
                    and "https://docs.example.org/format" in m["content"]
                    for m in messages
                ):
                    name, arguments = (
                        "web_search",
                        {"query": "public text formatting conventions"},
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
                    assert "user_feedback" not in payload["context"]
                    assert (
                        "evidence" not in payload["context"]["rubrics"]["criteria"][0]
                    )
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
    feedback = tmp_path / "feedback.md"
    feedback.write_text("User: Please write only the corrected answer in the file.")
    evidence = {
        "context_path": str(source),
        "files": [{"path": str(feedback), "role": "feedback"}],
    }
    # Each short-lived caller models an independent host session's Hook.
    script = """
import json, sys
from cosens.rsi import RSIClient
config = json.loads(sys.argv[4])["model"]
config["model"] += "-" + sys.argv[3]
c = RSIClient(data_dir=sys.argv[1], config={**config, "auto_start": True})
run = c.execution_create(evidence=json.loads(sys.argv[2]), base_recipe_id=None,
    idempotency_key='hook-' + sys.argv[3])
print(json.dumps({'id': run['id']}))
"""
    callers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(root),
                json.dumps(evidence),
                f"session-{index}",
                json.dumps(config),
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
        assert all(r["result"]["published"] for r in runs), runs
        assert peak_running == 2
        assert gateway.peak == 2  # Shared default model budget across processes.
        assert gateway.requests >= 40  # 5 stages, tool call + result, 4 runs.
        for index, run in enumerate(runs):
            material = client.material_get(run["material_ids"][0])
            assert (
                run["analysis"]["feedback"][0]["evidence_refs"][0]["locator"]
                == material["files"][1]["locator"]
            )
            test = run["test_results"][0]
            assert test["status"] == "passed"
            from pathlib import Path

            trace = json.loads(Path(test["trace_path"]).read_text())
            expected_model = f"local-test-session-{index}"
            assert run["runtime_config"]["model"]["model"] == expected_model
            assert trace["model"] == expected_model
            assert any(m["role"] == "tool" for m in trace["messages"])
            checkpoint = (
                client.data_dir / "executions" / run["id"] / "checkpoints-1.sqlite3"
            )
            with closing(
                sqlite3.connect(checkpoint.as_uri() + "?mode=ro", uri=True)
            ) as conn:
                namespaces = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT checkpoint_ns FROM checkpoints"
                    )
                }
            assert "" in namespaces
            assert not any(namespace.startswith("test:") for namespace in namespaces)
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


def test_recipe_generation_can_search_public_references(tmp_path, gateway, monkeypatch):
    from cosens.rsi import web_search

    monkeypatch.setenv("RSI_MODEL_API_KEY", "fixture")
    monkeypatch.setenv("COSENS_RSI_WEB_SEARCH_API_KEY", "fixture-search")
    monkeypatch.setenv("COSENS_RSI_WEB_SEARCH_ALLOWED_DOMAINS", "example.org")
    monkeypatch.setenv("COSENS_RSI_WEB_SEARCH_BLOCKED_DOMAINS", "blocked.example.org")
    calls = []

    def search(url, *, json, headers, timeout):
        assert url == "https://api.exa.ai/search" and timeout == 20
        assert headers == {"x-api-key": "fixture-search"}
        calls.append(json)
        return web_search.httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://docs.example.org/format",
                        "title": "Formatting",
                        "highlights": ["Public reference text"],
                    },
                    {
                        "url": "https://blocked.example.org/item",
                        "highlights": ["Blocked reference"],
                    },
                ]
            },
            request=web_search.httpx.Request("POST", url),
        )

    monkeypatch.setattr(web_search.httpx, "post", search)
    client = RSIClient(
        data_dir=tmp_path / "rsi",
        config={
            "model": {
                "model": "fixture",
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
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == "passed"
    assert calls == [
        {
            "query": "public text formatting conventions",
            "type": "auto",
            "numResults": 5,
            "contents": {"highlights": True},
            "includeDomains": ["example.org"],
            "excludeDomains": ["blocked.example.org"],
        }
    ]
    (trace,) = client.data_dir.glob("executions/*/input-*/generate-1.json")
    content = trace.read_text()
    assert "Public reference text" in content
    assert "Blocked reference" not in content
    assert "fixture-search" not in content


def test_recover_once_waits_for_a_stopped_workers_lease(tmp_path, gateway, monkeypatch):
    monkeypatch.setenv("RSI_MODEL_API_KEY", "fixture-not-a-live-secret")
    client = RSIClient(
        data_dir=tmp_path / "rsi",
        config={
            "model": {
                "model": "local-test",
                "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
            },
            "max_executions": 1,
        },
        auto_start=False,
    )
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    material = client.material_create(manifest={"context_path": str(context)})
    execution = client.execution_create(
        material_ids=[material["id"]],
        policy={
            "artifact_confirmed": True,
            "confirmation_ref": "recovery",
            "test_task": {
                "instruction": "Write 42 into answer.txt",
                "checks": [
                    {"kind": "text_equals", "path": "answer.txt", "value": "42"}
                ],
            },
        },
    )
    assert client.store.reserve("worker", "stopped-worker", 1, 3)
    assert client.store.claim("stopped-worker", 3, 1) == (execution["id"], 1)
    script = """
import json, sys
from cosens.rsi import RSIClient

print(json.dumps(RSIClient(data_dir=sys.argv[1], config=None).recover()))
"""
    hook = subprocess.run(
        [sys.executable, "-c", script, str(client.data_dir)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(hook.stdout) == {"started": 0, "scheduled": True}

    run = client.wait(execution["id"], timeout=20)
    assert run["status"] == "completed", run
    assert run["result"]["test_status"] == "passed"
    assert "lease-2" in run["test_results"][0]["trace_path"]


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
        [entry] = client.recipe_list()
        assert entry["execution_id"] == execution["id"]
        assert entry["recipe_id"] is None
        assert entry["status"] == "failed"
        assert (
            gateway.requests == 6
        )  # two extraction calls + analysis read and three malformed outputs
    else:
        assert result["status"] == "completed", result
        assert result["result"]["test_status"] == "passed"
        traces = list(client.data_dir.glob("executions/*/input-*/analysis.json"))
        trace = json.loads(traces[0].read_text())
        assert trace["structured_output_attempts"] == 2
