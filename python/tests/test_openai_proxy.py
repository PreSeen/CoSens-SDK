import json
import inspect
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import cosens


class Session:
    session_id = "chat-1"

    def __init__(self):
        self.items = [{"id": "old", "role": "user", "content": "previous"}]

    async def get_items(self):
        return self.items


def rsi_server(calls):
    def server(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((path, body))
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {"summary": "Check facts"}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {"wiki": "Frozen source"}})
        if path.endswith("/session-events"):
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    return httpx.MockTransport(server)


def test_disable_preserves_native_calls_and_full_resource_tree(tmp_path):
    native = OpenAI(api_key="test", base_url="https://model.example/v1")
    original = SimpleNamespace(output_text="native")
    seen = []
    native.responses.create = lambda **kwargs: (seen.append(kwargs), original)[1]
    native.files.list = lambda **kwargs: "files"
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    assert client.responses.create(model="model", input="hello") is original
    assert seen == [{"model": "model", "input": "hello"}]
    assert client.files.list() == "files"
    assert client.native is native
    assert str(client.native.base_url) == "https://model.example/v1/"
    assert client.with_options(timeout=3).cosens is not None


def test_public_openai_resource_tree_is_discoverable_and_signatures_delegate(tmp_path):
    native = OpenAI(api_key="test")
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    resources = [
        "responses", "chat", "completions", "files", "audio", "images", "embeddings",
        "batches", "models", "moderations", "vector_stores", "fine_tuning", "realtime", "beta",
    ]
    for resource in resources:
        assert resource in dir(client)
        assert getattr(client, resource) is not None
    assert inspect.signature(client.responses.create) == inspect.signature(native.responses.create)
    assert inspect.signature(client.chat.completions.create) == inspect.signature(native.chat.completions.create)


def test_auto_false_calls_native_once_and_never_contacts_cosens(tmp_path):
    native = OpenAI(api_key="test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":false,"objective":"","task_family":"general","reason":"routine"}')
        return SimpleNamespace(output_text="answer")

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server([]))
    assert client.responses.create(model="app", input="hello").output_text == "answer"
    assert [c["model"] for c in calls] == ["decision", "app"]
    assert client.cosens.rsi_capture == "skipped"


def test_auto_true_prepares_injects_context_and_submits_after_response(tmp_path):
    native = OpenAI(api_key="test")
    model_calls = []
    rsi_calls = []

    def create(**kwargs):
        model_calls.append(kwargs)
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"solve task","task_family":"general","reason":"multi-step"}')
        assert kwargs["instructions"].startswith("Original")
        assert "Frozen source" in kwargs["instructions"]
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(rsi_calls))
    result = client.responses.create(model="app", instructions="Original", input="current")
    assert result.output_text == "done"
    assert [c[0] for c in rsi_calls] == [
        "/v1/runs/prepare", "/v1/runs/r1", "/v1/runs/r1/session-events",
        "/v1/runs/r1/session-events", "/v1/runs/r1/submit",
    ]
    assert [e["id"] for e in rsi_calls[2][1]["events"]] == ["chat:old"]
    assert [e["kind"] for e in rsi_calls[3][1]["events"]] == ["user_prompt", "agent_action"]
    assert [e["payload"]["content"] for e in rsi_calls[3][1]["events"]] == ["current", "done"]
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"


def test_classifier_failure_does_not_block_native_call_or_advance_history(tmp_path):
    native = OpenAI(api_key="test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "decision":
            raise TimeoutError("classifier timeout")
        return SimpleNamespace(output_text="native")

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server([]))
    assert client.responses.create(model="app", input="current").output_text == "native"
    assert calls == ["decision", "app"]
    assert client.cosens.rsi_capture == "decision_failed"


@pytest.mark.asyncio
async def test_async_openai_generation_uses_same_atomic_flow(tmp_path):
    native = AsyncOpenAI(api_key="test")
    calls = []

    async def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    result = await client.responses.create(model="app", input="current")
    assert result.output_text == "done"
    assert calls[-1][0] == "/v1/runs/r1/submit"


def test_raw_response_is_returned_with_parse_hook_and_original_headers(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
    parsed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])
    raw = SimpleNamespace(headers={"x-model": "original"}, parse=lambda: parsed)
    native.responses.with_raw_response.create = lambda **kwargs: raw
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    wrapped = client.responses.with_raw_response.create(model="app", input="current")
    assert wrapped.headers is raw.headers
    assert "/v1/runs/r1/submit" not in [path for path, _ in calls]
    assert wrapped.parse() is parsed
    assert wrapped.parse() is parsed
    assert [path for path, _ in calls].count("/v1/runs/r1/submit") == 1
    assert calls[-1][0] == "/v1/runs/r1/submit"


def test_stream_true_submits_after_completed_event_and_preserves_iterator(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    completed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return iter([SimpleNamespace(type="response.created"), SimpleNamespace(type="response.completed", response=completed)])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    stream = client.responses.create(model="app", input="current", stream=True)
    assert "/v1/runs/r1/submit" not in [path for path, _ in calls]
    assert [event.type for event in stream] == ["response.created", "response.completed"]
    assert calls[-1][0] == "/v1/runs/r1/submit"


def test_context_managed_stream_keeps_completed_state_after_exit(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    completed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    class Manager:
        def __enter__(self):
            return iter([SimpleNamespace(type="response.completed", response=completed)])

        def __exit__(self, exc_type, exc, tb):
            return False

    native.responses.stream = lambda **kwargs: Manager()
    client = cosens(native, mode="auto", decision_model="decision", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    with client.responses.stream(model="app", input="current") as stream:
        assert [event.type for event in stream] == ["response.completed"]
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"


@pytest.mark.asyncio
async def test_async_stream_manager_prepares_on_enter_and_submits_on_completion(tmp_path):
    native = AsyncOpenAI(api_key="test")
    calls = []
    completed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    async def create(**kwargs):
        return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    class Manager:
        async def __aenter__(self):
            class Stream:
                async def __aiter__(self):
                    yield SimpleNamespace(type="response.completed", response=completed)
            return Stream()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    native.responses.create = create
    native.responses.stream = lambda **kwargs: Manager()
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    async with client.responses.stream(model="app", input="current") as stream:
        assert [event.type async for event in stream] == ["response.completed"]
    assert calls[-1][0] == "/v1/runs/r1/submit"


def test_chat_completions_keeps_original_messages_in_order_and_adds_context(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    model_calls = []

    def decide(**kwargs):
        model_calls.append(kwargs)
        return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    original_messages = [{"role": "system", "content": "Original policy"}, {"role": "user", "content": "Current question"}]

    def complete(**kwargs):
        model_calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="chat answer"))])

    native.responses.create = decide
    native.chat.completions.create = complete
    client = cosens(native, mode="auto", decision_model="decision", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    response = client.chat.completions.create(model="app", messages=original_messages)
    assert response.choices[0].message.content == "chat answer"
    assert original_messages == [{"role": "system", "content": "Original policy"}, {"role": "user", "content": "Current question"}]
    assert model_calls[1]["messages"][:2] == original_messages
    assert model_calls[1]["messages"][2]["role"] == "developer"
    assert "Current question" in model_calls[0]["input"]
    assert "Original policy" not in model_calls[0]["input"]
    assert calls[-1][0] == "/v1/runs/r1/submit"


def test_function_call_continuation_reuses_one_run_and_submits_only_final_message(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    native.responses.create = lambda **kwargs: (
        SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        if kwargs["model"] == "decision" else
        SimpleNamespace(id="tool-response", status="completed", output_text="", output=[SimpleNamespace(type="function_call")])
        if not kwargs.get("previous_response_id") else
        SimpleNamespace(id="final-response", status="completed", output_text="finished", output=[SimpleNamespace(type="message", role="assistant")])
    )
    client = cosens(native, mode="auto", decision_model="decision", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    client.responses.create(model="app", input="Call a tool")
    assert "/v1/runs/r1/submit" not in [path for path, _ in calls]
    client.responses.create(model="app", previous_response_id="tool-response", input=[{"type": "function_call_output", "call_id": "c1", "output": "tool result"}])
    assert [path for path, _ in calls].count("/v1/runs/prepare") == 1
    assert [path for path, _ in calls].count("/v1/runs/r1/submit") == 1
    reported = [event for path, body in calls if path.endswith("/session-events") for event in body["events"]]
    assert any(event["kind"] == "user_prompt" and event["payload"]["content"] == "Call a tool" for event in reported)
    assert any(event["kind"] == "agent_action" and event["payload"].get("source_revision") == "native_function_call" for event in reported)
    assert any(event["kind"] == "tool_result" and event["payload"]["content"] == "tool result" for event in reported)
