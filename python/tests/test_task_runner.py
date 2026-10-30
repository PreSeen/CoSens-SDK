import json
from types import SimpleNamespace

import httpx
import pytest
from agents import Runner

from cosens import TaskWorkflowRunner, cosens
from cosens.errors import RSIRecoveryRequired


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_real_agents_tool_approval_resumes_the_same_task_execution(tmp_path, streamed):
    from agents import Agent, Model, ModelResponse, RunConfig, RunResult, function_tool
    from agents.usage import Usage
    from openai.types.responses import Response, ResponseCompletedEvent, ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    class ScriptedModel(Model):
        def __init__(self):
            self.calls = []

        async def get_response(self, **kwargs):
            self.calls.append(kwargs)
            item = (ResponseFunctionToolCall(type="function_call", name="lookup", call_id="call-1", arguments='{"query":"fact"}')
                    if len(self.calls) == 1 else ResponseOutputMessage(id="msg-1", type="message", role="assistant", status="completed",
                        content=[ResponseOutputText(type="output_text", text="approved answer", annotations=[])]))
            return ModelResponse(output=[item], usage=Usage(), response_id=f"response-{len(self.calls)}")

        async def stream_response(self, system_instructions, input, *args, **kwargs):
            value = await self.get_response(system_instructions=system_instructions, input=input, **kwargs)
            yield ResponseCompletedEvent(type="response.completed", sequence_number=1,
                response=Response(id=value.response_id, created_at=0, model="scripted", object="response", status="completed",
                    output=value.output, parallel_tool_calls=False, tool_choice="auto", tools=[]))

    tool_calls = []
    @function_tool(needs_approval=True)
    def lookup(query: str) -> str:
        tool_calls.append(query)
        return "found"

    model = ScriptedModel()
    agent = Agent(name="helper", instructions="Original instruction", model=model, tools=[lookup])
    seen = []
    def server(request):
        path = request.url.path; seen.append(path)
        if path == "/api/v1/tasks": return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if path.endswith("/executions"): return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if path == "/api/v1/artifacts": return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if path.endswith("/result"): return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        raise AssertionError(path)

    wrapper = TaskWorkflowRunner(Runner, mode="auto", api_key="key", state_path=tmp_path / "native-approval.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    try:
        options = {"run_config": RunConfig(tracing_disabled=True),
            "rsi": {"turn_id": "approved-turn", "task": {"source": "new_task", "objective": "work"}}}
        if streamed:
            interrupted = wrapper.run_streamed(agent, "work", **options)
            async for _event in interrupted.stream_events():
                pass
            assert interrupted.is_complete
        else:
            interrupted = await wrapper.run(agent, "work", **options)
            assert isinstance(interrupted, RunResult)
        assert len(interrupted.interruptions) == 1
        assert tool_calls == [] and len(model.calls) == 1
        assert wrapper.cosens.get_turn("approved-turn")["state"] == "awaiting_native_approval"
        pending = await wrapper.pending_state(agent, "approved-turn")
        pending.approve(pending.get_interruptions()[0])
        resumed = await wrapper.resume(agent, "approved-turn", pending, run_config=RunConfig(tracing_disabled=True))
        assert isinstance(resumed, RunResult) and resumed.final_output == "approved answer"
        assert tool_calls == ["fact"] and len(model.calls) == 2
        assert wrapper.cosens.get_turn("approved-turn")["state"] == "completed"
        assert seen.count("/api/v1/tasks") == seen.count("/api/v1/tasks/t1/executions") == seen.count("/api/v1/executions/e1/result") == 1
        assert model.calls[0]["system_instructions"] == model.calls[1]["system_instructions"]
        assert agent.instructions == "Original instruction"
    finally:
        await wrapper.aclose()


class FakeAgent:
    instructions = "native instructions"

    def clone(self, *, instructions=None):
        result = FakeAgent()
        result.instructions = instructions
        return result


class FakeRunner:
    def __init__(self):
        self.calls = []

    async def run(self, agent, user_input, **options):
        self.calls.append((agent, user_input, options))
        return SimpleNamespace(final_output="native answer", interruptions=[])


@pytest.mark.asyncio
async def test_factory_selects_task_workflow_runner_and_disable_never_calls_gateway(tmp_path):
    wrapper = cosens(Runner, mode="disable", api_key="key", state_path=tmp_path / "factory.sqlite3")
    assert isinstance(wrapper, TaskWorkflowRunner)
    await wrapper.aclose()
    native = FakeRunner()
    requests = []
    wrapper = TaskWorkflowRunner(native, mode="disable", api_key="key", state_path=tmp_path / "disabled.sqlite3",
                                 _http_transport=httpx.MockTransport(lambda request: requests.append(request)))
    result = await wrapper.run(FakeAgent(), "work", rsi={"turn_id": "turn-1", "task": {"source": "new_task", "objective": "work"}})
    assert result.final_output == "native answer"
    assert len(native.calls) == 1
    assert requests == []
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_submits_once_and_reconciles_lost_result_receipt(tmp_path):
    class ObservedRunner(FakeRunner):
        async def run(self, agent, user_input, **options):
            result = await super().run(agent, user_input, **options)
            result.new_items = [SimpleNamespace(type="tool_call_output_item", raw_item={"output": "observed tool output"})]
            return result

    native = ObservedRunner()
    seen = []
    submissions = 0

    def server(request):
        nonlocal submissions
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        path = request.url.path
        if path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if path == "/api/v1/tasks/t1/executions":
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "frozen-hash", "request": {"kind": "original"}})
        if path == "/api/v1/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if path == "/api/v1/executions/e1/result":
            submissions += 1
            if submissions == 1:
                return httpx.Response(503, json={"error": {"code": "transient", "message": "receipt lost"}})
            return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        raise AssertionError(path)

    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "workflow.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    result = await wrapper.run(FakeAgent(), "work", rsi={"turn_id": "turn-1", "task": {"source": "new_task", "objective": "work"}})
    assert result.final_output == "native answer"
    assert "frozen task execution request" in native.calls[0][0].instructions
    assert wrapper.cosens.get_turn("turn-1")["state"] == "completion_pending"
    with pytest.raises(RSIRecoveryRequired):
        await wrapper.run(FakeAgent(), "work", rsi={"turn_id": "turn-1", "task": {"source": "new_task", "objective": "work"}})
    recovered = await wrapper.reconcile("turn-1")
    assert recovered["state"] == "completed" and recovered["revision_id"] == "r1"
    assert len(native.calls) == 1
    assert [path for path, _ in seen].count("/api/v1/artifacts") == 1
    bodies = [body for path, body in seen if path.endswith("/result")]
    assert bodies[0] == bodies[1]
    assert bodies[0]["request_sha256"] == "frozen-hash"
    assert bodies[0]["artifacts"] == [{"artifact_id": "a1", "role": "submitted_output"}]
    assert bodies[0]["events"] == [{"id": "native-item-1", "kind": "tool_call_output_item", "payload": {"item_type": "tool_call_output_item", "raw_item": {"output": "observed tool output"}}}]
    assert bodies[0]["trace_coverage"] == "partial"
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_waits_for_context_then_reuses_task_and_reports_observed_trace(tmp_path):
    native = FakeRunner()
    seen = []
    ready = False

    def server(request):
        nonlocal ready
        seen.append(request.url.path)
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "needs_context"})
        if request.url.path == "/api/v1/tasks/t1/context":
            ready = True
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path == "/api/v1/tasks/t1":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready" if ready else "needs_context"})
        if request.url.path == "/api/v1/tasks/t1/executions":
            return httpx.Response(200, json={"execution_id": "e1", "request_sha256": "h1", "request": {"kind": "reconstructed"}, "status": "awaiting_execution"})
        if request.url.path == "/api/v1/artifacts":
            return httpx.Response(200, json={"artifact_id": "new-a1", "content_hash": "server-hash"})
        if request.url.path == "/api/v1/executions/e1/result":
            assert json.loads(request.content)["events"] == [{"id": "event-1", "kind": "tool_result", "payload": {"name": "tool"}}]
            assert json.loads(request.content)["trace_coverage"] == "partial"
            return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "context.sqlite3",
                                 trace_adapter=lambda _: {"events": [{"id": "event-1", "kind": "tool_result", "payload": {"name": "tool"}}], "trace_coverage": "partial"},
                                 _http_transport=httpx.MockTransport(server))
    rsi = {"turn_id": "turn-1", "task": {"source": "external_artifact", "artifacts": ["external-output"]}}
    with pytest.raises(RSIRecoveryRequired, match="needs_context"):
        await wrapper.run(FakeAgent(), "work", rsi=rsi)
    assert wrapper.cosens.get_turn("turn-1")["task_id"] == "t1"
    assert native.calls == []
    await wrapper.cosens.atoms.supplement_task_context("t1", expected_context_version=1, objective="work", self_contained=True)
    result = await wrapper.run(FakeAgent(), "work", rsi=rsi)
    assert result.final_output == "native answer"
    assert wrapper.cosens.get_turn("turn-1")["revision_id"] == "r1"
    assert seen.count("/api/v1/tasks") == 1
    assert len(native.calls) == 1
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_stream_submits_after_native_stream_finishes(tmp_path):
    seen = []

    class Stream:
        is_complete = False
        final_output = "stream answer"
        interruptions = []
        run_loop_exception = None

        async def stream_events(self):
            yield {"type": "raw_response_event"}
            self.is_complete = True

    class Native:
        def run_streamed(self, *_args, **_kwargs):
            return Stream()

    def server(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if request.url.path == "/api/v1/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "stream.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    stream = wrapper.run_streamed(FakeAgent(), "work", rsi={"turn_id": "stream-1", "task": {"source": "new_task", "objective": "work"}})
    assert "/api/v1/artifacts" not in seen
    events = [event async for event in stream.stream_events()]
    assert events == [{"type": "raw_response_event"}]
    assert seen[-2:] == ["/api/v1/artifacts", "/api/v1/executions/e1/result"]
    assert wrapper.cosens.get_turn("stream-1")["state"] == "completed"
    await wrapper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_options", [{}, {"mode": "after_turn"}])
async def test_task_runner_cancelled_stream_never_submits(tmp_path, cancel_options):
    seen = []
    cancelled = []

    class Stream:
        is_complete = False
        def cancel(self, mode="immediate"):
            cancelled.append(mode)
            return "cancelled"

    class Native:
        def run_streamed(self, *_args, **_kwargs):
            return Stream()

    def server(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "cancel.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    stream = wrapper.run_streamed(FakeAgent(), "work", rsi={"turn_id": "cancel-1", "task": {"source": "new_task", "objective": "work"}})
    assert stream.cancel(**cancel_options) == "cancelled"
    assert cancelled == [cancel_options.get("mode", "immediate")]
    assert wrapper.cosens.get_turn("cancel-1")["state"] == "incomplete"
    assert seen == ["/api/v1/tasks", "/api/v1/tasks/t1/executions"]
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_does_not_replay_failed_native_execution(tmp_path):
    calls = 0

    class Native:
        async def run(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("native side effect unknown")

    def server(request):
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "failure.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    rsi = {"turn_id": "failed-turn", "task": {"source": "new_task", "objective": "work"}}
    with pytest.raises(RuntimeError, match="unknown"):
        await wrapper.run(FakeAgent(), "work", rsi=rsi)
    with pytest.raises(RSIRecoveryRequired, match="may have executed"):
        await wrapper.run(FakeAgent(), "work", rsi=rsi)
    assert calls == 1
    assert wrapper.cosens.get_turn("failed-turn")["state"] == "native_failed"
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_does_not_fabricate_an_artifact_for_missing_native_output(tmp_path):
    seen = []

    class Native:
        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(final_output=None, interruptions=[])

    def server(request):
        seen.append(request.url.path)
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "no-output.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    result = await wrapper.run(FakeAgent(), "work", rsi={"turn_id": "no-output", "task": {"source": "new_task", "objective": "work"}})
    assert result.final_output is None
    assert wrapper.cosens.get_turn("no-output")["state"] == "completion_pending"
    assert seen == ["/api/v1/tasks", "/api/v1/tasks/t1/executions"]
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_pre_execution_recovery_binds_input_and_session(tmp_path):
    seen = []

    def server(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"task_id": "t1", "status": "needs_context"})

    wrapper = TaskWorkflowRunner(FakeRunner(), mode="auto", api_key="key", state_path=tmp_path / "binding.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    rsi = {"turn_id": "turn-1", "task": {"source": "new_task", "objective": "work"}}
    session_1 = SimpleNamespace(session_id="s1", session_incarnation="first")
    session_2 = SimpleNamespace(session_id="s1", session_incarnation="second")
    with pytest.raises(RSIRecoveryRequired, match="needs_context"):
        await wrapper.run(FakeAgent(), "work", rsi=rsi, session=session_1)
    with pytest.raises(RSIRecoveryRequired, match="input or Session"):
        await wrapper.run(FakeAgent(), "different input", rsi=rsi, session=session_1)
    with pytest.raises(RSIRecoveryRequired, match="input or Session"):
        await wrapper.run(FakeAgent(), "work", rsi=rsi, session=session_2)
    assert seen == ["/api/v1/tasks"]
    await wrapper.aclose()


def test_task_runner_run_sync_uses_native_sync_runner_and_sync_gateway(tmp_path):
    seen = []
    native_calls = []
    result_submissions = 0

    class Native:
        async def run(self, *_args, **_kwargs):
            raise AssertionError("async Runner.run must not be used by run_sync")

        def run_sync(self, agent, user_input, **options):
            native_calls.append((agent, user_input, options))
            return SimpleNamespace(final_output="native sync answer", interruptions=[])

    def server(request):
        nonlocal result_submissions
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"task_id": "t1", "status": "ready"})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if request.url.path == "/api/v1/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if request.url.path.endswith("/result"):
            result_submissions += 1
            if result_submissions == 1:
                return httpx.Response(503, json={"error": {"message": "receipt lost"}})
            return httpx.Response(200, json={"status": "completed", "revision_id": "r1"})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "sync.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    native_result = wrapper.run_sync(FakeAgent(), "work", rsi={"turn_id": "sync-1", "task": {"source": "new_task", "objective": "work"}})
    assert native_result.final_output == "native sync answer"
    assert len(native_calls) == 1
    assert wrapper.cosens.get_turn("sync-1")["state"] == "completion_pending"
    recovered = wrapper.reconcile_sync("sync-1")
    assert recovered["state"] == "completed" and recovered["revision_id"] == "r1"
    assert len(native_calls) == 1
    assert [body for path, body in seen if path.endswith("/result")][0] == [body for path, body in seen if path.endswith("/result")][1]
    wrapper.close()
