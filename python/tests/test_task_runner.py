import json
from types import SimpleNamespace

import httpx
import pytest
from agents import Runner

from cosens import TaskWorkflowRunner, cosens
from cosens.errors import RSIHTTPError, RSIRecoveryRequired


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
        if path == "/api/tasks": return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if path.endswith("/executions"): return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if path == "/api/artifacts": return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if path.endswith("/result"): return httpx.Response(200, json={"status": "completed", "trace_origin": "original"})
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
        assert seen.count("/api/tasks") == seen.count("/api/executions") == seen.count("/api/executions/e1/result") == 1
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
        if path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if path == "/api/executions":
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "frozen-hash", "request": {"kind": "original"}})
        if path == "/api/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if path == "/api/executions/e1/result":
            submissions += 1
            if submissions == 1:
                return httpx.Response(503, json={"error": {"code": "transient", "message": "receipt lost"}})
            return httpx.Response(200, json={"status": "completed", "trace_origin": "original"})
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
    assert recovered["state"] == "completed" and "revision_id" not in recovered
    assert len(native.calls) == 1
    assert [path for path, _ in seen].count("/api/artifacts") == 1
    assert seen[:2] == [("/api/tasks", {"objective": "work", "artifacts": []}),
                        ("/api/executions", {"task_id": "t1", "source": "new_task", "input": {"objective": "work", "artifacts": []}})]
    bodies = [body for path, body in seen if path.endswith("/result")]
    assert bodies[0] == bodies[1]
    assert bodies[0]["request_sha256"] == "frozen-hash"
    assert bodies[0]["artifacts"] == [{"artifact_id": "a1", "role": "submitted_output"}]
    assert bodies[0]["events"] == [{"id": "native-item-1", "kind": "tool_call_output_item", "payload": {"item_type": "tool_call_output_item", "raw_item": {"output": "observed tool output"}}}]
    assert bodies[0]["trace_coverage"] == "partial"
    await wrapper.aclose()


class IncompleteServer:
    """Refuses POST /executions as input_incomplete while ``incomplete`` holds, naming what the input lacks as the
    service does, or answers ``execution_error`` instead; one task body per idempotency key."""

    def __init__(self):
        self.seen = []
        self.keys = {}
        self.task_bodies = {}
        self.incomplete = True
        self.execution_error = None

    @staticmethod
    def _missing(body):
        value = body["input"]
        roles = [("original_input" if body["source"] == "new_task" else "submitted_output") if isinstance(a, str) else a["role"]
                 for a in value["artifacts"]]
        missing = [] if value.get("objective") else ["objective"]
        if body["source"] == "external_artifact":
            if "original_input" not in roles and not value.get("self_contained"):
                missing.append("original_input_or_self_contained_task")
            if "submitted_output" not in roles:
                missing.append("submitted_output")
        return missing

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        key = request.headers.get("idempotency-key")
        self.seen.append((request.method, request.url.path, body))
        self.keys.setdefault(request.url.path, []).append(key)
        if request.url.path == "/api/tasks":
            if self.task_bodies.setdefault(key, body) != body:
                return httpx.Response(422, json={"error": {"code": "invalid_request", "message": "idempotency key reused with different task"}})
            return httpx.Response(200, json={"task_id": "t1", "objective": body["objective"], "artifacts": [], "context_version": 1})
        if request.url.path == "/api/tasks/t1":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if request.url.path == "/api/executions":
            if self.execution_error:
                return self.execution_error(request)
            if self.incomplete:
                return httpx.Response(422, json={"error": {"code": "input_incomplete", "message": "the execution input is incomplete",
                                                           "missing": self._missing(body)}})
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "reconstructed"}})
        if request.url.path == "/api/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if request.url.path == "/api/executions/e1/result":
            return httpx.Response(200, json={"status": "completed", "trace_origin": "reconstructed"})
        raise AssertionError(request.url.path)


class SyncCapableRunner(FakeRunner):
    def run_sync(self, agent, user_input, **options):
        self.calls.append((agent, user_input, options))
        return SimpleNamespace(final_output="native answer", interruptions=[])


def _runner_call(wrapper, synchronous):
    async def run(user_input, rsi, session=None):
        if synchronous:
            return wrapper.run_sync(FakeAgent(), user_input, rsi=rsi, session=session)
        return await wrapper.run(FakeAgent(), user_input, rsi=rsi, session=session)
    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_task_runner_raises_incomplete_input_unchanged_and_binds_the_retry(tmp_path, synchronous):
    server = IncompleteServer()
    native = SyncCapableRunner()
    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "incomplete.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    run = _runner_call(wrapper, synchronous)
    spec = {"source": "external_artifact", "objective": "work", "artifacts": ["external-output"]}
    rsi = {"turn_id": "turn-1", "task": spec}
    session_1 = SimpleNamespace(session_id="s1", session_incarnation="first")
    with pytest.raises(RSIHTTPError) as caught:
        await run("work", rsi, session_1)
    assert caught.value.status_code == 422 and caught.value.code == "input_incomplete"
    assert caught.value.missing == ["original_input_or_self_contained_task"] and caught.value.recipe_ids is None
    assert native.calls == [] and wrapper.cosens.get_turn("turn-1")["state"] == "pre_execution_pending"
    for user_input, session in (("different input", session_1), ("work", SimpleNamespace(session_id="s1", session_incarnation="second"))):
        with pytest.raises(RSIRecoveryRequired, match="input or Session"):
            await run(user_input, rsi, session)
    assert [path for _, path, _ in server.seen] == ["/api/tasks", "/api/executions"]
    server.incomplete = False
    result = await run("work", rsi, session_1)
    assert result.final_output == "native answer" and len(native.calls) == 1
    # The retry runs the same spec as the execution input of the same task, under the same keys.
    execution = {"task_id": "t1", "source": "external_artifact", "input": {"objective": "work", "artifacts": ["external-output"]}}
    assert server.seen[0][2] == {"objective": "work", "artifacts": []}
    assert [body for _, path, body in server.seen if path == "/api/executions"] == [execution, execution]
    assert len(set(server.keys["/api/executions"])) == 1
    # The sync runner kept the task it created; the async one asks for it again under the same key.
    assert len(server.keys["/api/tasks"]) == (1 if synchronous else 2) and len(set(server.keys["/api/tasks"])) == 1
    await wrapper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_task_runner_retries_an_incomplete_turn_with_a_corrected_spec(tmp_path, synchronous):
    server = IncompleteServer()
    native = SyncCapableRunner()
    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "corrected.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    run = _runner_call(wrapper, synchronous)
    incomplete = {"source": "external_artifact", "objective": "work", "artifacts": ["external-output"]}
    with pytest.raises(RSIHTTPError, match="incomplete"):
        await run("work", {"turn_id": "turn-1", "task": incomplete})
    server.incomplete = False
    corrected = {**incomplete, "self_contained": True}
    result = await run("work", {"turn_id": "turn-1", "task": corrected})
    assert result.final_output == "native answer" and len(native.calls) == 1
    turn = wrapper.cosens.get_turn("turn-1")
    assert turn["execution_id"] == "e1" and turn["task_spec"] == corrected and turn["task_id"] == "t1"
    assert "task_spec_incomplete" not in turn
    executions = [body for _, path, body in server.seen if path == "/api/executions"]
    assert executions[-1]["input"] == {"objective": "work", "artifacts": ["external-output"], "self_contained": True}
    # Once the turn has started an execution its spec is fixed again.
    with pytest.raises(RSIRecoveryRequired, match="may have executed"):
        await run("work", {"turn_id": "turn-1", "task": incomplete})
    await wrapper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_task_runner_keeps_the_objective_when_correcting_an_incomplete_spec(tmp_path, synchronous):
    server = IncompleteServer()
    native = SyncCapableRunner()
    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "objective.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    run = _runner_call(wrapper, synchronous)
    incomplete = {"source": "external_artifact", "objective": "work", "artifacts": ["external-output"]}
    with pytest.raises(RSIHTTPError, match="incomplete"):
        await run("work", {"turn_id": "turn-1", "task": incomplete})
    sent = len(server.seen)
    # The task was created and keyed from the objective, so a correction may complete only the execution input.
    with pytest.raises(RSIRecoveryRequired, match="objective changed"):
        await run("work", {"turn_id": "turn-1", "task": {**incomplete, "objective": "other work", "self_contained": True}})
    assert len(server.seen) == sent and wrapper.cosens.get_turn("turn-1")["task_spec"] == incomplete
    server.incomplete = False
    corrected = {**incomplete, "artifacts": ["external-output", {"artifact_id": "brief", "role": "original_input"}]}
    await run("work", {"turn_id": "turn-1", "task": corrected})
    assert wrapper.cosens.get_turn("turn-1")["execution_id"] == "e1" and len(native.calls) == 1
    assert {json.dumps(body, sort_keys=True) for _, path, body in server.seen if path == "/api/tasks"} == {
        json.dumps({"objective": "work", "artifacts": []}, sort_keys=True)}
    await wrapper.aclose()


def _transport_error(request):
    raise httpx.ReadTimeout("receipt lost", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("failure", [
    lambda request: httpx.Response(422, json={"error": {"code": "invalid_request", "message": "bad execution"}}),
    _transport_error], ids=["refused", "receipt_lost"])
async def test_task_runner_locks_the_spec_after_a_start_that_was_not_input_incomplete(tmp_path, synchronous, failure):
    server = IncompleteServer()
    native = SyncCapableRunner()
    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "locked.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    run = _runner_call(wrapper, synchronous)
    incomplete = {"source": "external_artifact", "objective": "work", "artifacts": ["external-output"]}
    with pytest.raises(RSIHTTPError, match="incomplete"):
        await run("work", {"turn_id": "turn-1", "task": incomplete})
    # The next start of the same spec fails some other way, so it may have started an execution.
    server.execution_error = failure
    with pytest.raises(Exception):
        await run("work", {"turn_id": "turn-1", "task": incomplete})
    assert "task_spec_incomplete" not in wrapper.cosens.get_turn("turn-1")
    sent = len(server.seen)
    with pytest.raises(RSIRecoveryRequired, match="Workflow task changed"):
        await run("work", {"turn_id": "turn-1", "task": {**incomplete, "self_contained": True}})
    assert len(server.seen) == sent and native.calls == []
    await wrapper.aclose()


@pytest.mark.asyncio
async def test_task_runner_refuses_a_corrected_spec_from_another_mode_without_recording_it(tmp_path):
    server = IncompleteServer()
    native = SyncCapableRunner()
    path = tmp_path / "mode.sqlite3"
    auto = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=path, _http_transport=httpx.MockTransport(server))
    incomplete = {"source": "external_artifact", "objective": "work", "artifacts": ["external-output"]}
    with pytest.raises(RSIHTTPError, match="incomplete"):
        await auto.run(FakeAgent(), "work", rsi={"turn_id": "turn-1", "task": incomplete})
    await auto.aclose()
    approval = TaskWorkflowRunner(native, mode="approval", api_key="key", state_path=path, _http_transport=httpx.MockTransport(server))
    with pytest.raises(RSIRecoveryRequired, match="different mode"):
        await approval.run(FakeAgent(), "work", rsi={"turn_id": "turn-1", "task": {**incomplete, "self_contained": True},
                                                     "authorize": lambda *args: True})
    await approval.aclose()
    auto = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=path, _http_transport=httpx.MockTransport(server))
    turn = auto.cosens.get_turn("turn-1")
    assert turn["task_spec"] == incomplete and turn["task_spec_incomplete"] is True
    await auto.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
@pytest.mark.parametrize("spec,message", [
    ({"objective": "work"}, "task.source is required"),
    ({"source": "external_artifact", "objective": "work"}, "external_artifact requires submitted_output"),
    ({"source": "new_task"}, "objective must be nonempty")])
async def test_task_runner_validates_the_spec_before_creating_a_task(tmp_path, synchronous, spec, message):
    server = IncompleteServer()
    native = SyncCapableRunner()
    wrapper = TaskWorkflowRunner(native, mode="auto", api_key="key", state_path=tmp_path / "invalid.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    run = _runner_call(wrapper, synchronous)
    with pytest.raises(ValueError, match=message):
        await run("work", {"turn_id": "turn-1", "task": spec})
    assert server.seen == [] and native.calls == [] and wrapper.cosens.get_turn("turn-1") is None
    # Nothing was recorded, so the same turn runs once the spec is fixed.
    server.incomplete = False
    await run("work", {"turn_id": "turn-1", "task": {"source": "new_task", "objective": "work", "self_contained": True}})
    assert wrapper.cosens.get_turn("turn-1")["execution_id"] == "e1"
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
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if request.url.path == "/api/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"status": "completed", "trace_origin": "original"})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "stream.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    stream = wrapper.run_streamed(FakeAgent(), "work", rsi={"turn_id": "stream-1", "task": {"source": "new_task", "objective": "work"}})
    assert "/api/artifacts" not in seen
    events = [event async for event in stream.stream_events()]
    assert events == [{"type": "raw_response_event"}]
    assert seen[-2:] == ["/api/artifacts", "/api/executions/e1/result"]
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
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "cancel.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    stream = wrapper.run_streamed(FakeAgent(), "work", rsi={"turn_id": "cancel-1", "task": {"source": "new_task", "objective": "work"}})
    assert stream.cancel(**cancel_options) == "cancelled"
    assert cancelled == [cancel_options.get("mode", "immediate")]
    assert wrapper.cosens.get_turn("cancel-1")["state"] == "incomplete"
    assert seen == ["/api/tasks", "/api/executions"]
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
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
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
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "no-output.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    result = await wrapper.run(FakeAgent(), "work", rsi={"turn_id": "no-output", "task": {"source": "new_task", "objective": "work"}})
    assert result.final_output is None
    assert wrapper.cosens.get_turn("no-output")["state"] == "completion_pending"
    assert seen == ["/api/tasks", "/api/executions"]
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
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json={"task_id": "t1", "objective": "work", "artifacts": [], "context_version": 1})
        if request.url.path.endswith("/executions"):
            return httpx.Response(200, json={"execution_id": "e1", "status": "awaiting_execution", "request_sha256": "h1", "request": {"kind": "original"}})
        if request.url.path == "/api/artifacts":
            return httpx.Response(200, json={"artifact_id": "a1", "content_hash": "server-hash"})
        if request.url.path.endswith("/result"):
            result_submissions += 1
            if result_submissions == 1:
                return httpx.Response(503, json={"error": {"message": "receipt lost"}})
            return httpx.Response(200, json={"status": "completed", "trace_origin": "original"})
        raise AssertionError(request.url.path)

    wrapper = TaskWorkflowRunner(Native(), mode="auto", api_key="key", state_path=tmp_path / "sync.sqlite3",
                                 _http_transport=httpx.MockTransport(server))
    native_result = wrapper.run_sync(FakeAgent(), "work", rsi={"turn_id": "sync-1", "task": {"source": "new_task", "objective": "work"}})
    assert native_result.final_output == "native sync answer"
    assert len(native_calls) == 1
    assert seen[:2] == [("/api/tasks", {"objective": "work", "artifacts": []}),
                        ("/api/executions", {"task_id": "t1", "source": "new_task", "input": {"objective": "work", "artifacts": []}})]
    assert wrapper.cosens.get_turn("sync-1")["state"] == "completion_pending"
    recovered = wrapper.reconcile_sync("sync-1")
    assert recovered["state"] == "completed" and "revision_id" not in recovered
    assert len(native_calls) == 1
    assert [body for path, body in seen if path.endswith("/result")][0] == [body for path, body in seen if path.endswith("/result")][1]
    wrapper.close()
