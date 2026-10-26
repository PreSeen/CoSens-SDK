import json
import inspect
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from cosens.errors import RSIApprovalUnavailable, RSIDecisionUnavailable, RSIRecoveryRequired, RSITransportError


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
        if path == "/api/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {"wiki": "Frozen source"}})
        if path.endswith("/session-events"):
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    return httpx.MockTransport(server)


def test_disable_preserves_native_calls_and_full_resource_tree(tmp_path, empty_recall):
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


def test_close_releases_cosens_resources_after_last_derived_client(tmp_path):
    native = OpenAI(api_key="test")
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    derived = client.with_options(timeout=3)
    transport = client._runtime.client._sync_transport.client
    client.close()
    assert not transport.is_closed
    derived.close()
    assert transport.is_closed


@pytest.mark.asyncio
async def test_sync_openai_client_can_read_session_inside_running_event_loop(tmp_path):
    native = OpenAI(api_key="test")

    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server([]))
    assert client.responses.create(model="app", input="hello").output_text == "done"


def test_auto_retry_after_prepare_receipt_loss_reuses_turn_and_blocks_uncertain_native_retry(tmp_path):
    native = OpenAI(api_key="test")
    prepared_keys = []
    native_calls = 0
    attempts = 0

    def create(**kwargs):
        nonlocal native_calls
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        native_calls += 1
        raise RuntimeError("model request outcome unknown")

    def server(request):
        nonlocal attempts
        if request.url.path.endswith("/prepare"):
            prepared_keys.append(request.headers["Idempotency-Key"])
            attempts += 1
            if attempts == 1:
                raise httpx.ReadError("receipt lost")
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if request.url.path == "/api/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        raise AssertionError(request.url.path)

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    with pytest.raises(RSITransportError):
        client.responses.create(model="app", input="hello")
    with pytest.raises(RuntimeError, match="outcome unknown"):
        client.responses.create(model="app", input="hello")
    with pytest.raises(RSIRecoveryRequired):
        client.responses.create(model="app", input="hello")
    assert prepared_keys == [prepared_keys[0], prepared_keys[0]]
    assert native_calls == 1


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


def test_rewrapping_same_proxy_rejects_conflicting_decision_model(tmp_path):
    native = OpenAI(api_key="test")
    client = cosens(native, mode="auto", decision_model="classifier-a", state_path=tmp_path / "state.sqlite3")
    assert cosens(client, mode="auto", decision_model="classifier-a") is client
    with pytest.raises(ValueError):
        cosens(client, mode="auto", decision_model="classifier-b")


def test_disable_delegates_real_openai_sdk_http_and_return_models(tmp_path, empty_recall):
    model_requests = []

    def model_server(request):
        model_requests.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m1", "object": "model", "created": 0, "owned_by": "owner"}]})
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={
                "id": "resp-1", "created_at": 0, "model": "m1", "object": "response", "output": [],
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "status": "completed",
            })
        raise AssertionError(request.url.path)

    native = OpenAI(api_key="test", base_url="https://model.example/v1", http_client=httpx.Client(transport=httpx.MockTransport(model_server)))
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    assert client.models.list().data[0].id == "m1"
    assert client.responses.create(model="m1", input="hello").id == "resp-1"
    assert model_requests == ["/v1/models", "/v1/responses"]


def test_auto_false_recalls_then_calls_native_once_without_run(tmp_path, empty_recall):
    native = OpenAI(api_key="test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":false,"objective":"","task_family":"general","reason":"routine"}')
        return SimpleNamespace(output_text="answer")

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server([]))
    assert client.responses.create(model="app", input="hello").output_text == "answer"
    assert [c["model"] for c in calls] == ["decision", "app"]
    assert client.cosens.rsi_capture == "skipped"
    assert empty_recall == ["hello"]


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
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(rsi_calls))
    result = client.responses.create(model="app", instructions="Original", input="current")
    assert result.output_text == "done"
    assert [c[0] for c in rsi_calls] == [
        "/api/v1/runs/prepare", "/api/v1/runs/r1", "/api/v1/runs/r1/session-events",
        "/api/v1/runs/r1/session-events", "/api/v1/runs/r1/submit",
    ]
    assert [e["id"] for e in rsi_calls[2][1]["events"]] == ["chat:old"]
    assert [e["kind"] for e in rsi_calls[3][1]["events"]] == ["user_prompt", "agent_action"]
    assert [e["payload"]["content"] for e in rsi_calls[3][1]["events"]] == ["current", "done"]
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"


def test_current_input_already_in_session_is_not_reported_twice(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    session = Session()
    session.items = [{"id": "current", "role": "user", "content": "current"}]

    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=session, api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    client.responses.create(model="app", input="current")
    user_events = [event for path, body in calls if path.endswith("/session-events") for event in body["events"] if event["kind"] == "user_prompt"]
    assert len(user_events) == 1
    assert user_events[0]["id"] == "chat:current"


def test_classifier_failure_still_recalls_before_native_call(tmp_path, empty_recall):
    native = OpenAI(api_key="test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "decision":
            raise TimeoutError("classifier timeout")
        return SimpleNamespace(output_text="native")

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server([]))
    assert client.responses.create(model="app", input="current").output_text == "native"
    assert calls == ["decision", "app"]
    assert client.cosens.rsi_capture == "decision_failed"


def test_approval_mode_stops_before_native_execution_when_endpoint_is_absent(tmp_path):
    native = OpenAI(api_key="test")
    native_app_calls = 0
    rsi_calls = []

    def create(**kwargs):
        nonlocal native_app_calls
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        native_app_calls += 1
        return SimpleNamespace(output_text="unexpected")

    native.responses.create = create
    client = cosens(native, mode="approval", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(rsi_calls))
    with pytest.raises(RSIApprovalUnavailable):
        client.responses.create(model="app", input="current")
    assert native_app_calls == 0
    assert rsi_calls == []


def test_approval_decision_failure_is_fail_closed(tmp_path):
    native = OpenAI(api_key="test")
    native.responses.create = lambda **kwargs: (_ for _ in ()).throw(TimeoutError("classifier timeout"))
    client = cosens(native, mode="approval", decision_model="decision", state_path=tmp_path / "state.sqlite3")
    with pytest.raises(RSIDecisionUnavailable):
        client.responses.create(model="app", input="current")


def test_decision_timeout_and_output_limit_are_configurable(tmp_path, empty_recall):
    native = OpenAI(api_key="test")
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":false,"objective":"","task_family":"general","reason":"routine"}')
        return SimpleNamespace(output_text="answer")

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", decision_timeout=1.5, decision_max_output_tokens=64, state_path=tmp_path / "state.sqlite3")
    client.responses.create(model="app", input="hello")
    assert calls[0]["timeout"] == 1.5
    assert calls[0]["max_output_tokens"] == 64


@pytest.mark.asyncio
async def test_async_openai_generation_uses_same_atomic_flow(tmp_path):
    native = AsyncOpenAI(api_key="test")
    calls = []

    async def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    result = await client.responses.create(model="app", input="current")
    assert result.output_text == "done"
    assert calls[-1][0] == "/api/v1/runs/r1/submit"


def test_raw_response_is_returned_with_parse_hook_and_original_headers(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
    parsed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])
    raw = SimpleNamespace(headers={"x-model": "original"}, parse=lambda: parsed)
    native.responses.with_raw_response.create = lambda **kwargs: raw
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    wrapped = client.responses.with_raw_response.create(model="app", input="current")
    assert wrapped.headers is raw.headers
    assert "/api/v1/runs/r1/submit" not in [path for path, _ in calls]
    assert wrapped.parse() is parsed
    assert wrapped.parse() is parsed
    assert [path for path, _ in calls].count("/api/v1/runs/r1/submit") == 1
    assert calls[-1][0] == "/api/v1/runs/r1/submit"


def test_stream_true_submits_after_completed_event_and_preserves_iterator(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    completed = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    def create(**kwargs):
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        return iter([SimpleNamespace(type="response.created"), SimpleNamespace(type="response.completed", response=completed)])

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    stream = client.responses.create(model="app", input="current", stream=True)
    assert "/api/v1/runs/r1/submit" not in [path for path, _ in calls]
    assert [event.type for event in stream] == ["response.created", "response.completed"]
    assert calls[-1][0] == "/api/v1/runs/r1/submit"


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
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
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
    client = cosens(native, mode="auto", decision_model="decision", session=Session(), api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    async with client.responses.stream(model="app", input="current") as stream:
        assert [event.type async for event in stream] == ["response.completed"]
    assert calls[-1][0] == "/api/v1/runs/r1/submit"


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
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    response = client.chat.completions.create(model="app", messages=original_messages)
    assert response.choices[0].message.content == "chat answer"
    assert original_messages == [{"role": "system", "content": "Original policy"}, {"role": "user", "content": "Current question"}]
    assert model_calls[1]["messages"][:2] == original_messages
    assert model_calls[1]["messages"][2]["role"] == "developer"
    assert "Current question" in model_calls[0]["input"]
    assert "Original policy" not in model_calls[0]["input"]
    assert calls[-1][0] == "/api/v1/runs/r1/submit"


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
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    client.responses.create(model="app", input="Call a tool")
    assert "/api/v1/runs/r1/submit" not in [path for path, _ in calls]
    client.responses.create(model="app", previous_response_id="tool-response", input=[{"type": "function_call_output", "call_id": "c1", "output": "tool result"}])
    assert [path for path, _ in calls].count("/api/v1/runs/prepare") == 1
    assert [path for path, _ in calls].count("/api/v1/runs/r1/submit") == 1
    reported = [event for path, body in calls if path.endswith("/session-events") for event in body["events"]]
    assert any(event["kind"] == "user_prompt" and event["payload"]["content"] == "Call a tool" for event in reported)
    assert any(event["kind"] == "agent_action" and event["payload"].get("source_revision") == "native_function_call" for event in reported)
    assert any(event["kind"] == "tool_result" and event["payload"]["content"] == "tool result" for event in reported)


def test_openai_reconcile_completed_run_does_not_call_native_again(tmp_path):
    native = OpenAI(api_key="test")
    native_calls = 0
    submitted = None

    def create(**kwargs):
        nonlocal native_calls
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        native_calls += 1
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    def server(request):
        nonlocal submitted
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/api/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "completed" if submitted else "ready", "snapshot": {}, "result": submitted})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            submitted = json.loads(request.content)["data"]
            return httpx.Response(503, json={"error": {"code": "response_lost", "message": "unknown"}})
        raise AssertionError(path)

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    assert client.responses.create(model="app", input="current").output_text == "done"
    turn_id = client.cosens.last_turn_id
    assert client.cosens.get_turn(turn_id)["state"] == "completion_pending"
    assert client.cosens.reconcile(turn_id)["state"] == "completed"
    assert native_calls == 1


@pytest.mark.parametrize("second_decision", ["failed", "negative"])
def test_unresolved_turn_blocks_passthrough_after_classifier_change(tmp_path, second_decision):
    native = OpenAI(api_key="test")
    decision_calls = 0
    native_calls = 0

    def create(**kwargs):
        nonlocal decision_calls, native_calls
        if kwargs["model"] == "decision":
            decision_calls += 1
            if decision_calls > 1 and second_decision == "failed":
                raise TimeoutError("decision unavailable")
            active = decision_calls == 1
            return SimpleNamespace(output_text=json.dumps({"activate": active, "objective": "task", "task_family": "general", "reason": "needed"}))
        native_calls += 1
        return SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/api/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [event["id"] for event in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(503, json={"error": {"message": "receipt unknown"}})
        raise AssertionError(path)

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    client.responses.create(model="app", input="current")
    with pytest.raises(RSIRecoveryRequired):
        client.responses.create(model="app", input="current")
    assert (decision_calls, native_calls) == (1, 1)


def test_repeated_continuation_cannot_reexecute_after_uncertain_submit(tmp_path):
    native = OpenAI(api_key="test")
    native_calls = 0

    def create(**kwargs):
        nonlocal native_calls
        if kwargs["model"] == "decision":
            return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')
        native_calls += 1
        if not kwargs.get("previous_response_id"):
            return SimpleNamespace(id="tool-response", status="completed", output_text="", output=[SimpleNamespace(type="function_call")])
        return SimpleNamespace(id="final-response", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])

    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/api/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [event["id"] for event in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(503, json={"error": {"message": "receipt unknown"}})
        raise AssertionError(path)

    native.responses.create = create
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    client.responses.create(model="app", input="use tool")
    continuation = {"model": "app", "previous_response_id": "tool-response", "input": [{"type": "function_call_output", "call_id": "c1", "output": "result"}]}
    client.responses.create(**continuation)
    with pytest.raises(RSIRecoveryRequired):
        client.responses.create(**continuation)
    assert native_calls == 2


def test_chat_stream_chunk_events_submit_after_final_stop_chunk(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    class Manager:
        def __enter__(self):
            return iter([
                SimpleNamespace(type="chunk", chunk=SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="hello"), finish_reason=None)])),
                SimpleNamespace(type="chunk", chunk=SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" world"), finish_reason="stop")])),
            ])

        def __exit__(self, exc_type, exc, tb):
            return False

    native.chat.completions.stream = lambda **kwargs: Manager()
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    with client.chat.completions.stream(model="app", messages=[{"role": "user", "content": "task"}]) as stream:
        assert len(list(stream)) == 2
    assert calls[-1][0] == "/api/v1/runs/r1/submit"
    assert calls[-1][1]["data"]["summary"] == "hello world"


def test_stream_get_final_response_also_records_completion(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    final = SimpleNamespace(id="resp-1", status="completed", output_text="done", output=[SimpleNamespace(type="message", role="assistant")])
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    class Stream:
        def get_final_response(self):
            return final

        def __iter__(self):
            yield SimpleNamespace(type="response.completed", response=final)

    class Manager:
        def __enter__(self):
            return Stream()

        def __exit__(self, exc_type, exc, tb):
            return False

    native.responses.stream = lambda **kwargs: Manager()
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    with client.responses.stream(model="app", input="current") as stream:
        assert stream.get_final_response() is final
        assert len(list(stream)) == 1
    assert [path for path, _ in calls].count("/api/v1/runs/r1/submit") == 1


@pytest.mark.parametrize("raw_mode", ["with_raw_response", "with_streaming_response"])
def test_real_openai_raw_stream_parse_supports_next_and_records_terminal_event(tmp_path, raw_mode):
    rsi_calls = []
    sse = (
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"message","id":"msg-1","status":"completed","role":"assistant",'
        '"content":[{"type":"output_text","text":"done","annotations":[]}]}],'
        '"model":"m","object":"response","created_at":0,"parallel_tool_calls":false,'
        '"tool_choice":"auto","tools":[]}}\n\ndata: [DONE]\n\n'
    )
    def model_server(request):
        body = json.loads(request.content)
        if body["model"] == "decision":
            decision = '{"activate":true,"objective":"task","task_family":"general","reason":"needed"}'
            return httpx.Response(200, json={
                "id": "decision-1", "created_at": 0, "model": "decision", "object": "response", "status": "completed",
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                "output": [{"type": "message", "id": "decision-message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": decision, "annotations": []}]}],
            })
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    model_http = httpx.Client(transport=httpx.MockTransport(model_server))
    native = OpenAI(api_key="test", base_url="https://model.example/v1", http_client=model_http)
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(rsi_calls))
    raw = getattr(client.responses, raw_mode).create(model="m", input="task", stream=True)
    if raw_mode == "with_streaming_response":
        with raw as response:
            stream = response.parse()
            assert next(stream).type == "response.completed"
    else:
        stream = raw.parse()
        assert next(stream).type == "response.completed"
        stream.close()
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"
    assert [path for path, _ in rsi_calls].count("/api/v1/runs/r1/submit") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_mode", ["with_raw_response", "with_streaming_response"])
async def test_real_async_openai_raw_stream_parse_supports_anext(tmp_path, raw_mode):
    rsi_calls = []
    sse = (
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"resp-1","status":"completed",'
        '"output":[{"type":"message","id":"msg-1","status":"completed","role":"assistant",'
        '"content":[{"type":"output_text","text":"done","annotations":[]}]}],'
        '"model":"m","object":"response","created_at":0,"parallel_tool_calls":false,'
        '"tool_choice":"auto","tools":[]}}\n\ndata: [DONE]\n\n'
    )

    def model_server(request):
        body = json.loads(request.content)
        if body["model"] == "decision":
            decision = '{"activate":true,"objective":"task","task_family":"general","reason":"needed"}'
            return httpx.Response(200, json={
                "id": "decision-1", "created_at": 0, "model": "decision", "object": "response", "status": "completed",
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                "output": [{"type": "message", "id": "decision-message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": decision, "annotations": []}]}],
            })
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    model_http = httpx.AsyncClient(transport=httpx.MockTransport(model_server))
    native = AsyncOpenAI(api_key="test", base_url="https://model.example/v1", http_client=model_http)
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(rsi_calls))
    if raw_mode == "with_streaming_response":
        async with client.responses.with_streaming_response.create(model="m", input="task", stream=True) as response:
            stream = await response.parse()
            assert (await anext(stream)).type == "response.completed"
    else:
        raw = await client.responses.with_raw_response.create(model="m", input="task", stream=True)
        stream = raw.parse()
        assert (await anext(stream)).type == "response.completed"
        await stream.close()
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"
    assert [path for path, _ in rsi_calls].count("/api/v1/runs/r1/submit") == 1
    await client.close()


def test_unparsed_streaming_response_closure_marks_turn_incomplete(tmp_path):
    native = OpenAI(api_key="test")
    calls = []
    native.responses.create = lambda **kwargs: SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    class Manager:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, exc_type, exc, tb):
            return False

    native.responses.with_streaming_response.create = lambda **kwargs: Manager()
    client = cosens(native, mode="auto", decision_model="decision", api_key="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=rsi_server(calls))
    with client.responses.with_streaming_response.create(model="m", input="task", stream=True):
        pass
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "incomplete"
    assert not any(path.endswith("/submit") for path, _ in calls)


def test_legacy_completion_prompt_without_roles_is_explicitly_unsupported_for_rsi(tmp_path):
    native = OpenAI(api_key="test")
    decision_calls = 0

    def decide(**kwargs):
        nonlocal decision_calls
        decision_calls += 1
        return SimpleNamespace(output_text='{"activate":true,"objective":"task","task_family":"general","reason":"needed"}')

    native.responses.create = decide
    original = SimpleNamespace(choices=[SimpleNamespace(text="answer", finish_reason="stop")])
    native.completions.create = lambda **kwargs: original
    client = cosens(native, mode="auto", decision_model="decision", state_path=tmp_path / "state.sqlite3")
    assert client.completions.create(model="legacy", prompt="system-like instructions and user text") is original
    assert decision_calls == 0


def test_unknown_generation_method_clears_prior_capture_status(tmp_path):
    native = OpenAI(api_key="test")
    native.responses.future_generate = lambda **kwargs: "native-result"
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    client.cosens.rsi_capture = "completed"
    assert client.responses.future_generate(model="m") == "native-result"
    assert client.cosens.rsi_capture == "unsupported"
    assert client.cosens.rsi_capture == "unsupported"
