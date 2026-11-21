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
from concurrent.futures import ThreadPoolExecutor
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

        def __init__(self, *args):
            super().__init__(*args)
            self.rejected_keys = {}
            self.model_requests = []
            self.rejection_started = threading.Event()
            self.release_rejection = None
            self.ask_clarification = False
            self.test_timeout_key = None

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
                api_key = self.headers.get("X-Api-Key")
                server.model_requests.append(
                    (api_key, request["model"], request.get("max_tokens"))
                )
            try:
                rejection = server.rejected_keys.get(api_key)
                if rejection is not None:
                    server.rejection_started.set()
                    if server.release_rejection is not None:
                        server.release_rejection.wait(timeout=10)
                    self.send_error(rejection)
                    return
                time.sleep(0.03)
                model = request["model"]
                if "-session-" in model:
                    session = model.rsplit("-", 1)[-1]
                    if session == "0":
                        assert (
                            self.headers["Authorization"] == "Bearer fixture-gateway-0"
                        )
                        assert self.headers["X-Auth-Type"] == "access_token"
                        assert "X-Api-Key" not in self.headers
                    else:
                        assert (
                            self.headers["X-Api-Key"] == "fixture-provider-" + session
                        )
                        assert "Authorization" not in self.headers
                messages = request["messages"]
                system = messages[0]["content"]
                if (
                    server.test_timeout_key == api_key
                    and api_key is not None
                    and "recipe test executor" in system
                ):
                    time.sleep(0.9)
                    return
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
                long_analysis = (
                    "Analysis" in tools and catalog[0]["size"] > 4 * 1024 * 1024
                )
                pages = (
                    [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
                    if long_analysis
                    else []
                )
                if long_analysis and len(pages) < 3:
                    name, arguments = (
                        "read_evidence",
                        {"locator": catalog[0]["locator"], "limit": 512},
                    )
                    if len(pages) == 1:
                        arguments["offset"] = pages[0]["next_offset"]
                    if len(pages) == 2:
                        arguments["query"] = "TAIL_FINAL_RULE"
                elif not called:
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
                    if long_analysis:
                        assert pages[0]["text_omitted"] is True
                        assert pages[1]["offset"] == pages[0]["next_offset"]
                        assert pages[2]["match_offset"] > 4 * 1024 * 1024
                        assert "TAIL_FINAL_RULE" in pages[2]["text"]
                        assert pages[2]["end_offset"] == pages[2]["total_chars"]
                    assert payload["context"]["user_feedback"] == feedback
                    locator = catalog[0]["locator"]
                    needs_answer = (
                        server.ask_clarification and not payload["context"]["answers"]
                    )
                    name, arguments = (
                        "Analysis",
                        {
                            "recipe_name": "Write checked answers",
                            "sufficient_information": not needs_answer,
                            "hypothesis": "Assume decimal format"
                            if needs_answer
                            else None,
                            "questions": [
                                {
                                    "id": "format",
                                    "question": "Which number format?",
                                    "assumption": "decimal",
                                    "options": [
                                        {"id": "decimal", "label": "Decimal"},
                                        {"id": "hex", "label": "Hexadecimal"},
                                    ],
                                }
                            ]
                            if needs_answer
                            else [],
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
                    corrected = any(
                        m["role"] == "tool" and "matching its schema" in m["content"]
                        for m in messages
                    )
                    name, arguments = (
                        "RecipeContent",
                        {
                            "name": "Write 42",
                            "instructions": "Write 42 into answer.txt using write_output",
                        },
                    )
                    if corrected:
                        arguments["description"] = "Fixed answer task"
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
    monkeypatch.delenv("RSI_MODEL_API_KEY", raising=False)
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
    long_source = tmp_path / "long-context.jsonl"
    long_source.write_text(
        "session trace\n" * 330_000 + "TAIL_FINAL_RULE: Write only 42.\n"
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
session = sys.argv[3].rsplit("-", 1)[-1]
config["headers"] = ({"Authorization": "Bearer fixture-gateway-0", "X-Auth-Type": "access_token"}
    if session == "0" else {"X-Api-Key": "fixture-provider-" + session})
c = RSIClient(data_dir=sys.argv[1], config={**config, "auto_start": True})
run = c.execution_create(evidence=json.loads(sys.argv[2]),
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
                json.dumps(
                    {**evidence, "context_path": str(long_source)}
                    if index == 3
                    else evidence
                ),
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
        assert 1 <= gateway.peak <= 2  # Shared default model budget across processes.
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
            assert "fixture-gateway" not in json.dumps(run["runtime_config"])
            assert "fixture-provider" not in json.dumps(run["runtime_config"])
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


@pytest.mark.parametrize("deferred", [False, True])
def test_recover_recipe_leaves_other_recipes_queued(tmp_path, gateway, deferred):
    from cosens.rsi import RSIClient as HostClient

    config = {
        "model": "local-test",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "headers": {"X-Api-Key": "fixture"},
        "auto_start": False,
    }
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    client = HostClient(data_dir=tmp_path / "rsi", config=config)
    evidence = {"context_path": str(context), "files": []}
    other = client.execution_create(evidence=evidence, idempotency_key="other")
    target = client.execution_create(evidence=evidence, idempotency_key="target")
    assert client.recover(recipe_id="recipe_unknown") == {"started": 0}
    store = client._queue.store

    if deferred:
        for owner in ("stopped-worker", "stopped-peer"):
            assert store.reserve("worker", owner, 2, 3)
        assert store.claim("stopped-worker", 3, 2, recipe_id=target["recipe_id"]) == (
            target["id"],
            1,
        )

    restored = HostClient(data_dir=client.data_dir, config=config)
    assert restored.recover(recipe_id=target["recipe_id"]) == (
        {"started": 0, "scheduled": True} if deferred else {"started": 1}
    )
    completed = restored._queue.wait(target["id"], timeout=20)
    assert completed["status"] == "completed", completed
    assert completed["result"]["test_status"] == "passed"
    assert completed["recipe_id"] == target["recipe_id"]

    # Wait for the scoped Worker to finish draining before invoking unscoped recovery.
    deadline = time.monotonic() + 5
    while True:
        with store.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM slots WHERE kind='worker' AND expires>=? LIMIT 1",
                (time.time(),),
            ).fetchone()
        if not active:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert restored.execution_get(other["id"])["status"] == "queued"
    assert restored.recover(recipe_id=target["recipe_id"]) == {"started": 0}
    assert restored.recover() == {"started": 1}
    assert restored._queue.wait(other["id"], timeout=20)["status"] == "completed"


@pytest.mark.parametrize("scoped", [True, False])
def test_explicit_recover_refreshes_rejected_credentials(tmp_path, gateway, scoped):
    from cosens.rsi import RSIClient as HostClient

    gateway.rejected_keys["fixture-expired"] = 401
    initial_config = {
        "model": "model-before-refresh",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "headers": {"X-Api-Key": "fixture-expired"},
        "auto_start": False,
    }
    if scoped:
        initial_config["credential_scope"] = "account-primary"
    root = tmp_path / "rsi"
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    evidence = {"context_path": str(context), "files": []}
    initial = HostClient(data_dir=root, config=initial_config)
    target = initial.execution_create(evidence=evidence, idempotency_key="target")
    other_client = HostClient(
        data_dir=root,
        config={**initial_config, "credential_scope": "account-secondary"},
    )
    other = other_client.execution_create(evidence=evidence, idempotency_key="other")

    assert initial.recover(recipe_id=target["recipe_id"]) == {"started": 1}
    failed = initial._queue.wait(target["id"], timeout=20)
    assert failed["status"] == "failed"
    assert failed["error"]["upstream_status"] == 401
    assert initial.recover(recipe_id=target["recipe_id"]) == {"started": 0}

    latest = HostClient(
        data_dir=root,
        config={
            **initial_config,
            "credential_scope": "account-primary",
            "headers": {"X-Api-Key": "fixture-refreshed"},
            "model": "model-after-refresh",
            "max_tokens": 2048,
            "timeout": 35,
        },
    )
    proof = {}
    if not scoped:
        assert latest.recover() == {"started": 0}
        assert latest.recover(previous_headers={"X-Api-Key": "wrong"}) == {"started": 0}
        proof = {"previous_headers": {"x-api-key": "fixture-expired"}}
    assert latest.recover(**proof) == {"started": 1}
    result = latest._queue.wait(target["id"], timeout=20)
    assert result["status"] == "completed", result
    assert result["recipe_id"] == target["recipe_id"]
    assert result["error"] is None
    assert result["runtime_config"]["model"]["timeout"] == 35
    assert result["result"]["test_status"] == "passed"
    assert latest.execution_get(other["id"])["status"] == "queued"
    assert ("fixture-refreshed", "model-after-refresh", 2048) in gateway.model_requests
    stored = json.dumps(result["runtime_config"])
    assert "account-primary" not in stored
    assert "fixture-refreshed" not in stored


def test_live_worker_hands_off_expired_credentials_after_public_recover(
    tmp_path, gateway
):
    from cosens.rsi import RSIClient as HostClient

    gateway.rejected_keys["fixture-expired"] = 403
    gateway.release_rejection = threading.Event()
    config = {
        "model": "local-test",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "headers": {"X-Api-Key": "fixture-expired"},
        "credential_scope": "account-primary",
        "auto_start": False,
    }
    root = tmp_path / "rsi"
    initial = HostClient(data_dir=root, config=config)
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    target = initial.execution_create(
        evidence={"context_path": str(context), "files": []}, idempotency_key="target"
    )
    assert initial.recover() == {"started": 1}
    latest = HostClient(
        data_dir=root,
        config={
            **config,
            "headers": {"X-Api-Key": "fixture-refreshed"},
            "model": "replacement-model",
        },
    )
    try:
        assert gateway.rejection_started.wait(timeout=10)
        assert latest.recover(recipe_id=target["recipe_id"]) == {"started": 1}
        staged = latest._queue.execution_get(target["id"])
        assert staged["status"] == "running"
        assert staged["runtime_config"]["model"]["model"] == "local-test"
        assert staged["recovery_config"]["model"]["model"] == "replacement-model"
    finally:
        gateway.release_rejection.set()

    result = latest._queue.wait(target["id"], timeout=20)
    assert result["status"] == "completed", result
    assert result["error"] is None
    assert result["result"]["test_status"] == "passed"
    assert "recovery_config" not in result
    assert ("fixture-refreshed", "replacement-model", None) in gateway.model_requests
    assert "lease-2" in result["test_results"][0]["trace_path"]


@pytest.mark.parametrize("replay", [False, True])
def test_clarification_dispatch_uses_refreshed_headers_for_the_same_recipe(
    tmp_path, gateway, replay
):
    from cosens.rsi import RSIClient as HostClient

    gateway.ask_clarification = True
    config = {
        "model": "local-test",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "headers": {"X-Api-Key": "fixture-before-clarification"},
        "credential_scope": "account-primary",
        "max_tokens": 2048,
        "timeout": 23,
        "auto_start": True,
    }
    root = tmp_path / "rsi"
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    evidence = {"context_path": str(context), "files": []}
    original = HostClient(data_dir=root, config=config)
    first = original.execution_create(
        evidence=evidence, idempotency_key="clarification"
    )
    foreign_config = {
        **config,
        "credential_scope": "account-secondary",
        "headers": {"X-Api-Key": "fixture-other-account"},
    }
    foreign = HostClient(data_dir=root, config={**foreign_config, "auto_start": False})
    unrelated = foreign.execution_create(
        evidence=evidence, idempotency_key="other-account"
    )
    parent = original._queue.wait(first["id"], timeout=20)
    assert parent["status"] == "completed", parent
    assert parent["result"]["provisional"] is True
    deadline = time.monotonic() + 5
    while True:
        with original._queue.store.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM slots WHERE kind='worker' AND expires>=? LIMIT 1",
                (time.time(),),
            ).fetchone()
        if not active:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    (question,) = parent["questions"]
    answer = {
        "question_id": question["id"],
        "question_revision": question["revision"],
        "answer": ["decimal"],
    }
    gateway.rejected_keys["fixture-before-clarification"] = 401
    request_cutoff = len(gateway.model_requests)
    fresh_config = {
        **config,
        "base_url": config["base_url"] + "/",
        "headers": {"X-Api-Key": "fixture-after-clarification"},
        "max_tokens": 4096,
        "timeout": 61,
    }
    fresh = HostClient(data_dir=root, config=fresh_config)

    if replay:
        stale = HostClient(data_dir=root, config={**config, "auto_start": False})
        queued = stale.execution_clarification_answer(parent["id"], **answer)
        queued_id = queued["continuation_execution_id"]
        for changes in (
            foreign_config,
            {"model": "different-model"},
            {"base_url": config["base_url"] + "/other"},
        ):
            differing = HostClient(
                data_dir=root, config={**fresh_config, **changes, "auto_start": False}
            )
            assert (
                differing.execution_clarification_answer(parent["id"], **answer)
                == queued
            )
            untouched = original._queue.execution_get(queued_id)
            assert untouched["status"] == "queued"
            assert untouched["runtime_config"] == parent["runtime_config"]

    response = fresh.execution_clarification_answer(parent["id"], **answer)
    if replay:
        assert response == queued
    child = fresh._queue.wait(response["continuation_execution_id"], timeout=20)
    assert child["status"] == "completed", child
    assert child["base_execution_id"] == parent["id"]
    assert child["recipe_id"] == parent["recipe_id"]
    assert child["base_recipe_version_id"] == parent["recipe_version_id"]
    assert child["result"]["published"] is True
    expected_runtime = {
        **parent["runtime_config"],
        "model": {
            **parent["runtime_config"]["model"],
            "api_key_env": fresh._queue.config.model.api_key_env,
        },
    }
    assert child["runtime_config"] == expected_runtime
    assert fresh.execution_get(unrelated["id"])["status"] == "queued"
    continuation_requests = gateway.model_requests[request_cutoff:]
    assert continuation_requests
    assert set(continuation_requests) == {
        ("fixture-after-clarification", "local-test", 2048)
    }
    assert fresh.execution_clarification_answer(parent["id"], **answer) == response


@pytest.mark.parametrize("scoped", [True, False])
def test_recover_retry_key_resumes_timed_out_checkpoint_once(
    tmp_path, gateway, monkeypatch, scoped
):
    from cosens.rsi import InvalidState
    from cosens.rsi import RSIClient as HostClient

    gateway.test_timeout_key = "fixture-timed-out"
    config = {
        "model": "local-test",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "headers": {"X-Api-Key": gateway.test_timeout_key},
        "max_tokens": 2048,
        "timeout": 0.45,
        "auto_start": True,
    }
    if scoped:
        config["credential_scope"] = "account-primary"
    directory = tmp_path / "rsi"
    initial = HostClient(data_dir=directory, config=config)
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: answer-writer\ndescription: Write an answer\n---\n\nWrite the answer 42.\n"
    )
    recipe = initial.recipe_insert(skill)
    context = tmp_path / "context.md"
    context.write_text("Write the answer 42 into answer.txt")
    evidence = {"context_path": str(context), "files": []}
    job = initial.execution_create(
        evidence=evidence,
        idempotency_key="model-timeout",
        recipe_reference={"path": str(skill), "recipe_id": recipe["id"]},
    )
    recipe_id = job["recipe_id"]
    execution_id = job["id"]
    failed = initial._queue.wait(execution_id, timeout=20)
    assert failed["status"] == "failed", failed
    assert failed["error"]["code"] == "timeout"
    assert failed["error"]["stage"] == "test"
    assert (
        initial.recipe_get(recipe_id, version="active")["selected_version"]["id"]
        == recipe["active_version_id"]
    )
    assert initial.recover(recipe_id=recipe_id) == {"started": 0}

    store = initial._queue.store
    deadline = time.monotonic() + 5
    while True:
        with store.connect() as conn:
            active = conn.execute(
                "SELECT 1 FROM slots WHERE kind='worker' AND expires>=? LIMIT 1",
                (time.time(),),
            ).fetchone()
        if not active:
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)

    fresh_token = "fixture-refreshed" if scoped else gateway.test_timeout_key
    gateway.test_timeout_key = None
    fresh_config = {
        **config,
        "base_url": config["base_url"] + "/",
        "headers": {"X-Api-Key": fresh_token},
        "max_tokens": 4096,
        "timeout": 25,
    }
    fresh = HostClient(data_dir=directory, config=fresh_config)
    assert not hasattr(fresh, "recipe_retry")
    with pytest.raises(InvalidState, match="requires recipe_id"):
        fresh.recover(retry_key="retry")
    with pytest.raises(InvalidState, match="previous_headers"):
        fresh.recover(recipe_id=recipe_id, retry_key="retry", previous_headers={})
    with pytest.raises(ValueError):
        fresh.recover(recipe_id=recipe_id, retry_key="")
    for changes in (
        {"credential_scope": "account-secondary"},
        {"model": "different-model"},
        {"base_url": config["base_url"] + "/other"},
    ):
        wrong = HostClient(data_dir=directory, config={**fresh_config, **changes})
        with pytest.raises(InvalidState):
            wrong.recover(recipe_id=recipe_id, retry_key="retry")
    queue_only = HostClient(
        data_dir=directory, config={**fresh_config, "auto_start": False}
    )
    unrelated = queue_only.execution_create(
        evidence=evidence, idempotency_key="unrelated"
    )

    def fail_launch(*args, **kwargs):
        raise OSError("fixture launch error")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", fail_launch)
        with pytest.raises(OSError, match="fixture launch error"):
            queue_only.recover(recipe_id=recipe_id, retry_key="retry")
    queued = fresh._queue.execution_get(execution_id)
    assert queued["status"] == "queued"
    assert queued["stage"] == "test"
    assert queued["error"] is None
    assert queued["material_ids"] == failed["material_ids"]
    with pytest.raises(InvalidState):
        fresh.recover(recipe_id=recipe_id, retry_key="duplicate-new-key")

    actual_spawn = subprocess.Popen
    launches = []

    def count_launch(command, *args, **kwargs):
        launches.append(command)
        return actual_spawn(command, *args, **kwargs)

    request_cutoff = len(gateway.model_requests)
    retry_receipt = {"started": 0, "recipe_id": recipe_id, "execution_id": execution_id}
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", count_launch)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(client.recover, recipe_id=recipe_id, retry_key="retry")
                for client in (fresh, queue_only)
            ]
            results = sorted((future.result() for future in futures), key=len)
        assert results == [retry_receipt, {**retry_receipt, "scheduled": True}]
        assert len(launches) == 1
        assert "--retry-fence" in launches[0]
    result = fresh._queue.wait(execution_id, timeout=20)
    assert result["status"] == "completed", result
    assert result["result"]["published"] is True
    assert result["recipe_version_id"] == failed["recipe_version_id"]
    assert result["input_revision"] == failed["input_revision"]
    assert result["runtime_config"]["model"]["timeout"] == 0.45
    assert set(gateway.model_requests[request_cutoff:]) == {
        (fresh_token, "local-test", 2048)
    }
    assert fresh.execution_get(unrelated["id"])["status"] == "queued"
    assert fresh.recover(recipe_id=recipe_id, retry_key="retry") == retry_receipt
    with pytest.raises(InvalidState):
        fresh.recover(recipe_id=recipe_id, retry_key="already-published")


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
