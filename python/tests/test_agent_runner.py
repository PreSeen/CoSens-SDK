import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent

from cosens import cosens
from cosens.errors import RSIRecoveryRequired, RSITransportError


class Session:
    session_id = "s1"

    def __init__(self):
        self.items = [{"id": "old", "role": "user", "content": "Earlier request"}]

    async def get_items(self):
        return self.items


class Runner:
    calls = 0
    seen_agent = None

    @classmethod
    async def run(cls, agent, user_input, *, session=None, **options):
        cls.calls += 1
        cls.seen_agent = agent
        session.items.extend([
            {"id": "new-user", "role": "user", "content": user_input},
            {"id": "new-assistant", "role": "assistant", "content": "done"},
        ])
        return SimpleNamespace(final_output="done")


@pytest.mark.asyncio
async def test_explicit_run_composes_atoms_around_one_native_runner_call(tmp_path):
    calls = []

    def server(request):
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.path, body))
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {"summary": "Use checks"}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {"wiki": "Source fact"}})
        if path.endswith("/session-events"):
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed", "evaluation": "inconclusive", "accepted": None})
        raise AssertionError(path)

    Runner.calls = 0
    agent = Agent(name="assistant", instructions="Original instructions")
    session = Session()
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    result = await client.run(agent, "Current request", session=session)
    assert result.final_output == "done"
    assert Runner.calls == 1
    assert agent.instructions == "Original instructions"
    assert Runner.seen_agent is not agent
    assert "Source fact" in Runner.seen_agent.instructions
    assert [path for path, _ in calls] == [
        "/v1/runs/prepare", "/v1/runs/r1", "/v1/runs/r1/session-events",
        "/v1/runs/r1/session-events", "/v1/runs/r1/submit",
    ]
    assert [e["id"] for e in calls[2][1]["events"]] == ["chat:old"]
    assert [e["id"] for e in calls[3][1]["events"]] == ["chat:new-user", "chat:new-assistant"]
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"
    await client.aclose()


@pytest.mark.asyncio
async def test_pre_execution_event_failure_never_runs_agent_or_advances_cursor(tmp_path):
    calls = []

    def server(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if request.url.path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        return httpx.Response(503, json={"error": {"code": "unavailable", "message": "later"}})

    Runner.calls = 0
    session = Session()
    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    with pytest.raises(Exception):
        await client.run(agent, "Current request", session=session)
    assert Runner.calls == 0
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "pre_execution_pending"
    await client.aclose()


@pytest.mark.asyncio
async def test_post_execution_submit_failure_keeps_native_result_and_blocks_reexecution(tmp_path):
    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "later"}})
        raise AssertionError(path)

    Runner.calls = 0
    session = Session()
    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    result = await client.run(agent, "Current request", session=session)
    assert result.final_output == "done"
    assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completion_pending"
    with pytest.raises(RSIRecoveryRequired):
        await client.run(agent, "Current request", session=session)
    assert Runner.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_streamed_run_submits_only_after_native_stream_finishes(tmp_path):
    calls = []

    def server(request):
        path = request.url.path
        calls.append(path)
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    class Stream:
        final_output = "streamed answer"
        is_complete = True

        async def stream_events(self):
            yield "chunk"

    class StreamingRunner:
        @staticmethod
        def run_streamed(agent, user_input, *, session=None, **options):
            return Stream()

    session = Session()
    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=StreamingRunner)
    stream = await client.run_streamed(agent, "Current request", session=session)
    assert "/v1/runs/r1/submit" not in calls
    assert [event async for event in stream.stream_events()] == ["chunk"]
    assert calls[-1] == "/v1/runs/r1/submit"
    await client.aclose()


@pytest.mark.asyncio
async def test_processing_prepare_retries_with_same_key_before_native_run(tmp_path):
    prepare_keys = []

    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            prepare_keys.append(request.headers["idempotency-key"])
            if len(prepare_keys) == 1:
                return httpx.Response(200, json={"status": "processing", "retry_after_seconds": 0})
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    Runner.calls = 0
    session = Session()
    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    result = await client.run(agent, "Current request", session=session)
    assert result.final_output == "done"
    assert len(prepare_keys) == 2
    assert prepare_keys[0] == prepare_keys[1]
    assert Runner.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_retry_after_pre_execution_failure_reuses_run_and_frozen_recipe(tmp_path):
    counts = {"prepare": 0, "events": 0}

    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            counts["prepare"] += 1
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {"summary": "Frozen recipe"}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            counts["events"] += 1
            if counts["events"] == 1:
                return httpx.Response(503, json={"error": {"code": "unavailable", "message": "later"}})
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    Runner.calls = 0
    session = Session()
    agent = Agent(name="assistant", instructions="Original")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    with pytest.raises(Exception):
        await client.run(agent, "Current request", session=session)
    result = await client.run(agent, "Current request", session=session)
    assert result.final_output == "done"
    assert counts["prepare"] == 1
    assert Runner.calls == 1
    assert "Frozen recipe" in Runner.seen_agent.instructions
    await client.aclose()


@pytest.mark.asyncio
async def test_reconcile_lost_submit_response_uses_server_result_without_rerunning_agent(tmp_path):
    server_completed = False
    submitted_data = None

    def server(request):
        nonlocal server_completed, submitted_data
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "completed" if server_completed else "ready", "snapshot": {}, "result": submitted_data})
        if path.endswith("/session-events"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            submitted_data = json.loads(request.content)["data"]
            server_completed = True
            return httpx.Response(503, json={"error": {"code": "response_lost", "message": "unknown"}})
        raise AssertionError(path)

    Runner.calls = 0
    session = Session()
    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=Runner)
    await client.run(agent, "Current request", session=session)
    turn_id = client.cosens.last_turn_id
    assert client.cosens.get_turn(turn_id)["state"] == "completion_pending"
    assert (await client.cosens.reconcile(turn_id))["state"] == "completed"
    assert Runner.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_explicit_run_uploads_real_input_and_output_artifacts(tmp_path):
    calls = []
    upload_count = 0

    def server(request):
        nonlocal upload_count
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        calls.append((path, body))
        if path == "/v1/artifacts":
            upload_count += 1
            return httpx.Response(200, json={"artifact_id": f"a{upload_count}", "content_hash": f"h{upload_count}", "name": body["name"], "size_bytes": len(body["content"])})
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": "r1", "status": "completed"})
        raise AssertionError(path)

    class FileRunner:
        @staticmethod
        async def run(agent, user_input, *, session=None, **options):
            return SimpleNamespace(final_output={"summary": "done", "files": [{"name": "out.txt", "content": "answer"}]})

    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=FileRunner)
    await client.run(agent, "Current request", input_artifacts=[{"name": "input.txt", "content": "facts"}])
    assert [path for path, _ in calls] == [
        "/v1/artifacts", "/v1/runs/prepare", "/v1/runs/r1", "/v1/artifacts", "/v1/runs/r1/submit",
    ]
    assert calls[1][1]["input_artifact_ids"] == ["a1"]
    assert calls[-1][1]["data"]["files"] == [{"artifact_id": "a2", "content_hash": "h2"}]
    await client.aclose()


@pytest.mark.asyncio
async def test_lost_output_artifact_receipt_is_not_blindly_uploaded_again(tmp_path):
    uploads = 0

    def server(request):
        nonlocal uploads
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        if path == "/v1/artifacts":
            uploads += 1
            return httpx.Response(503, json={"error": {"code": "unknown", "message": "receipt lost"}})
        raise AssertionError(path)

    class FileRunner:
        @staticmethod
        async def run(agent, user_input, *, session=None, **options):
            return SimpleNamespace(final_output={"summary": "done", "files": [{"name": "out.txt", "content": "answer"}]})

    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=FileRunner)
    await client.run(agent, "Current request")
    with pytest.raises(RSIRecoveryRequired):
        await client.cosens.reconcile(client.cosens.last_turn_id)
    assert uploads == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_runs_for_same_session_serialize_history_uploads(tmp_path):
    reported = []
    run_number = 0

    async def server(request):
        nonlocal run_number
        path = request.url.path
        if path.endswith("/prepare"):
            run_number += 1
            return httpx.Response(200, json={"run_id": f"r{run_number}", "status": "ready", "recipe": {}, "context": {}})
        if path in {"/v1/runs/r1", "/v1/runs/r2"}:
            return httpx.Response(200, json={"run_id": path.rsplit("/", 1)[1], "status": "ready", "snapshot": {}})
        if path.endswith("/session-events"):
            await asyncio.sleep(0.01)
            body = json.loads(request.content)
            reported.extend(event["id"] for event in body["events"])
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": path.split("/")[3], "status": "completed"})
        raise AssertionError(path)

    class SlowRunner:
        @staticmethod
        async def run(agent, user_input, *, session=None, **options):
            await asyncio.sleep(0.01)
            session.items.append({"id": user_input, "role": "user", "content": user_input})
            return SimpleNamespace(final_output=user_input)

    agent = Agent(name="assistant")
    session = Session()
    client = cosens(agent, mode="disable", session=session, token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=SlowRunner)
    first, second = await asyncio.gather(
        client.run(agent, "one", session=session),
        client.run(agent, "two", session=session),
    )
    assert {first.final_output, second.final_output} == {"one", "two"}
    assert reported.count("chat:old") == 1
    assert reported.count("chat:one") == 1
    assert reported.count("chat:two") == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_native_failure_is_not_retried_automatically_after_possible_tool_effects(tmp_path):
    calls = 0

    def server(request):
        path = request.url.path
        if path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if path == "/v1/runs/r1":
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})
        raise AssertionError(path)

    class FailingRunner:
        @staticmethod
        async def run(agent, user_input, *, session=None, **options):
            nonlocal calls
            calls += 1
            raise RuntimeError("tool may have run")

    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=FailingRunner)
    with pytest.raises(RuntimeError):
        await client.run(agent, "Current request")
    with pytest.raises(RSIRecoveryRequired):
        await client.run(agent, "Current request")
    assert calls == 1


@pytest.mark.asyncio
async def test_completed_identical_user_request_starts_a_new_run(tmp_path):
    prepared = 0
    native_calls = 0

    def server(request):
        nonlocal prepared
        path = request.url.path
        if path.endswith("/prepare"):
            prepared += 1
            return httpx.Response(200, json={"run_id": f"r{prepared}", "status": "ready", "recipe": {}, "context": {}})
        if path in {"/v1/runs/r1", "/v1/runs/r2"}:
            return httpx.Response(200, json={"run_id": path.rsplit("/", 1)[-1], "status": "ready", "snapshot": {}})
        if path.endswith("/submit"):
            return httpx.Response(200, json={"run_id": path.split("/")[3], "status": "completed"})
        raise AssertionError(path)

    class RunnerStub:
        @staticmethod
        async def run(agent, user_input, *, session=None, **options):
            nonlocal native_calls
            native_calls += 1
            return SimpleNamespace(final_output="done")

    agent = Agent(name="assistant")
    client = cosens(agent, mode="disable", token="ordinary", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server), _runner=RunnerStub)
    await client.run(agent, "same task")
    first_turn = client.cosens.last_turn_id
    await client.run(agent, "same task")
    second_turn = client.cosens.last_turn_id
    assert (prepared, native_calls) == (2, 2)
    assert first_turn != second_turn
    assert client.cosens.get_turn(first_turn)["run_id"] == "r1"
    assert client.cosens.get_turn(second_turn)["run_id"] == "r2"
    await client.aclose()
