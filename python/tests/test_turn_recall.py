import contextlib
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from cosens.errors import RSIRecoveryRequired, RSITransportError


def service(calls, *, fail_read=False, snapshot=None):
    attempts = 0
    def server(request):
        nonlocal attempts
        path = request.url.path
        calls.append(path)
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {"summary": "original recipe"}, "context": {"source": "original context"}})
        if path == "/api/v1/runs/r1":
            attempts += 1
            if fail_read and attempts == 1:
                raise httpx.ReadError("snapshot unavailable")
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": snapshot if snapshot is not None else {"wiki": "original memory"}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"status": "completed"})
        raise AssertionError(path)
    return httpx.MockTransport(server)


def model_reply(index):
    if index == 1:
        return SimpleNamespace(id="tool-response", status="completed", output_text="", output=[SimpleNamespace(type="function_call", call_id="c1")])
    return SimpleNamespace(id="final-response", status="completed", output_text="done", output=[SimpleNamespace(type="message")])


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("new_mode", ["disable", "approval"])
@pytest.mark.parametrize("interrupted_at", ["prepare", "tool_call"])
@pytest.mark.asyncio
async def test_recovery_cannot_reuse_auto_decision_under_another_mode(tmp_path, asynchronous, new_mode, interrupted_at):
    calls, business, decisions = [], [], []
    request = {"model": "app", "input": "task"}

    def create(**kwargs):
        if kwargs["model"] == "decision":
            decisions.append(kwargs)
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        business.append(kwargs)
        return model_reply(len(business))

    async def acreate(**kwargs):
        return create(**kwargs)

    def fail_prepare(request):
        calls.append(request.url.path)
        raise httpx.ReadError("prepare receipt unknown")

    def wrap(mode, transport):
        native = AsyncOpenAI(api_key="model") if asynchronous else OpenAI(api_key="model")
        native.responses.create = acreate if asynchronous else create
        return cosens(native, mode=mode, decision_model="decision", api_key="gateway-test",
                      state_path=tmp_path / "state.sqlite3", _http_transport=transport)

    async def invoke(client, kwargs):
        if asynchronous:
            return await client.responses.create(**kwargs)
        return client.responses.create(**kwargs)

    async def close(client):
        if asynchronous:
            await client.close()
        else:
            client.close()

    first = wrap("auto", httpx.MockTransport(fail_prepare) if interrupted_at == "prepare" else service(calls))
    try:
        if interrupted_at == "prepare":
            with pytest.raises(RSITransportError):
                await invoke(first, request)
        else:
            await invoke(first, request)
            request = {"model": "app", "previous_response_id": "tool-response",
                       "input": [{"type": "function_call_output", "call_id": "c1", "output": "result"}]}
    finally:
        await close(first)

    before = (len(calls), len(business), len(decisions))
    restored = wrap(new_mode, service(calls))
    try:
        with pytest.raises(RSIRecoveryRequired, match="mode"):
            await invoke(restored, request)
        assert (len(calls), len(business), len(decisions)) == before
    finally:
        await close(restored)


@pytest.mark.parametrize("reference", ["previous_response_id", "scope", "conversation"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_tool_loop_reuses_recipe_memory_and_decision_once(tmp_path, reference, asynchronous):
    native = AsyncOpenAI(api_key="test") if asynchronous else OpenAI(api_key="test")
    calls, business, decisions = [], [], []
    def create(**kwargs):
        if kwargs["model"] == "decision":
            decisions.append(kwargs)
            return SimpleNamespace(output_text=json.dumps({"activate": len(decisions) == 1, "objective": "task", "task_family": "general", "reason": "needed"}))
        business.append(kwargs)
        return model_reply(len(business))
    async def acreate(**kwargs):
        return create(**kwargs)
    native.responses.create = acreate if asynchronous else create
    client = cosens(native, mode="auto", decision_model="decision", api_key="test-gateway", state_path=tmp_path / "state.sqlite3", _http_transport=service(calls))
    first = {"model": "app", "input": "task", "instructions": "application"}
    second = {"model": "app", "input": [{"type": "function_call_output", "call_id": "c1", "output": "result"}], "instructions": "application"}
    if reference == "previous_response_id":
        second["previous_response_id"] = "tool-response"
    elif reference == "conversation":
        first["conversation"] = second["conversation"] = "conv-1"
    scope = client.cosens.scope(turn_id="explicit-turn") if reference == "scope" else contextlib.nullcontext()
    try:
        with scope:
            if asynchronous:
                await client.responses.create(**first)
                await client.responses.create(**second)
            else:
                client.responses.create(**first)
                client.responses.create(**second)
        assert len(decisions) == 1
        assert len(business) == 2
        assert business[0]["instructions"] == business[1]["instructions"]
        assert all("original recipe" in b["instructions"] and "original memory" in b["instructions"] for b in business)
        assert calls.count("/api/v1/runs/prepare") == 1
        assert calls.count("/api/v1/runs/r1") == 1
        assert calls.count("/api/v1/runs/r1/submit") == 1
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


def test_read_retry_keeps_original_prepared_recipe(tmp_path):
    calls, business = [], []
    native = OpenAI(api_key="test")
    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        business.append(kwargs)
        return model_reply(2)
    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", state_path=tmp_path / "state.sqlite3", _http_transport=service(calls, fail_read=True))
    try:
        with pytest.raises(RSITransportError):
            client.responses.create(model="app", input="task")
        assert business == []
        client.responses.create(model="app", input="task")
        assert "original recipe" in business[0]["instructions"]
        assert calls.count("/api/v1/runs/prepare") == 1
    finally:
        client.close()


@pytest.mark.parametrize("payload", [
    {"run_id": "r1", "status": "ready"},
    {"run_id": "r1", "status": "failed", "snapshot": {}},
    {"run_id": "other", "status": "ready", "snapshot": {}},
])
def test_incomplete_or_mismatched_snapshot_never_reaches_business_model(tmp_path, payload):
    native = OpenAI(api_key="test")
    business = []
    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        business.append(kwargs)
        return model_reply(2)
    def server(request):
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        return httpx.Response(200, json=payload)
    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    try:
        with pytest.raises(RSIRecoveryRequired):
            client.responses.create(model="app", input="task")
        assert business == []
    finally:
        client.close()


@pytest.mark.asyncio
async def test_agent_approval_resume_reuses_durable_snapshot(tmp_path):
    from agents import Agent
    calls, instructions = [], []
    class NativeState:
        def to_json(self):
            return {"approval": "pending"}
    class Runner:
        @staticmethod
        async def run(agent, user_input, **kwargs):
            instructions.append(agent.instructions)
            if isinstance(user_input, str):
                return SimpleNamespace(interruptions=["pending"], to_state=NativeState, final_output=None)
            return SimpleNamespace(interruptions=[], final_output="done")
    agent = Agent(name="assistant", instructions="application")
    options = {"mode": "disable", "state_path": tmp_path / "state.sqlite3", "_http_transport": service(calls), "_runner": Runner}
    client = cosens(agent, **options)
    await client.run(agent, "task")
    turn_id = client.cosens.last_turn_id
    await client.aclose()
    restored = cosens(agent, **options)
    try:
        await restored.resume(agent, turn_id, NativeState())
        assert instructions[0] == instructions[1]
        assert "original recipe" in instructions[1] and "original memory" in instructions[1]
        assert calls.count("/api/v1/runs/r1") == 1
    finally:
        await restored.aclose()
