import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import CoSensClient, cosens
from cosens.atoms import AsyncAtoms, SyncAtoms
from cosens.errors import RSIError, RSITransportError
from cosens.models import result


def context():
    # Normalized adapter result; no remote HTTP schema is assumed by this fixture.
    return result({"recipe": {"summary": "recall recipe"}, "context": {"source": "evidence-1"}, "snapshot": {"wiki": "recall memory"}})


@pytest.mark.parametrize("mode", ["disable", "auto_false", "auto_failure"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_missing_recall_contract_blocks_business_model_without_creating_run(tmp_path, mode, asynchronous):
    business, requests = [], []
    native = AsyncOpenAI(api_key="model") if asynchronous else OpenAI(api_key="model")
    def create(**kwargs):
        if kwargs["model"] == "decision":
            if mode == "auto_failure":
                raise TimeoutError("classification failed")
            return SimpleNamespace(output_text='{"activate":false,"objective":"task","task_family":"general","reason":"routine"}')
        business.append(kwargs)
        return SimpleNamespace(output_text="answer")
    async def acreate(**kwargs):
        return create(**kwargs)
    native.responses.create = acreate if asynchronous else create
    client = cosens(native, mode="disable" if mode == "disable" else "auto", decision_model="decision", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(500))[1]))
    try:
        with pytest.raises(RSIError, match="recall"):
            if asynchronous:
                await client.responses.create(model="business", input="task")
            else:
                client.responses.create(model="business", input="task")
        assert business == []
        assert requests == []
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.asyncio
async def test_atomic_recall_boundary_reports_missing_contract_without_fabricated_http():
    requests = []
    async with CoSensClient(_http_transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(500))[1])) as client:
        assert callable(getattr(client.atoms, "recall_context", None))
        with pytest.raises(RSIError, match="recall"):
            await client.atoms.recall_context(query="task")
        with pytest.raises(RSIError, match="recall"):
            client.sync_atoms.recall_context(query="task")
    assert requests == []


@pytest.mark.parametrize("mode", ["disable", "auto_false", "auto_failure"])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_independent_context_is_injected_once_per_turn_without_run_events(tmp_path, monkeypatch, mode, asynchronous):
    recalls, business, decisions, requests = [], [], [], []
    def recall(self, *, query):
        recalls.append(query)
        return context()
    async def arecall(self, *, query):
        return recall(self, query=query)
    monkeypatch.setattr(SyncAtoms, "recall_context", recall, raising=False)
    monkeypatch.setattr(AsyncAtoms, "recall_context", arecall, raising=False)
    native = AsyncOpenAI(api_key="model") if asynchronous else OpenAI(api_key="model")
    def create(**kwargs):
        if kwargs["model"] == "decision":
            decisions.append(kwargs)
            if mode == "auto_failure":
                raise TimeoutError("classification failed")
            return SimpleNamespace(output_text='{"activate":false,"objective":"","task_family":"general","reason":"routine"}')
        business.append(kwargs)
        if len(business) == 1:
            return SimpleNamespace(id="tool-response", status="completed", output_text="", output=[SimpleNamespace(type="function_call")])
        return SimpleNamespace(id="finished", status="completed", output_text="answer", output=[SimpleNamespace(type="message")])
    async def acreate(**kwargs):
        return create(**kwargs)
    native.responses.create = acreate if asynchronous else create
    client = cosens(native, mode="disable" if mode == "disable" else "auto", decision_model="decision", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(500))[1]))
    try:
        for kwargs in [
            {"input": "first turn"},
            {"previous_response_id": "tool-response", "input": [{"type": "function_call_output", "call_id": "c1", "output": "result"}]},
            {"input": "second turn"},
        ]:
            if asynchronous:
                await client.responses.create(model="business", instructions="original", **kwargs)
            else:
                client.responses.create(model="business", instructions="original", **kwargs)
        assert recalls == ["first turn", "second turn"]
        assert len(decisions) == (0 if mode == "disable" else 2)
        assert all(b["instructions"].startswith("original") and "recall recipe" in b["instructions"] and "recall memory" in b["instructions"] for b in business)
        assert requests == []
        state = client.cosens.get_turn(client.cosens.last_turn_id)
        assert state["run_id"] is None
        assert state["state"] == "completed"
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


def test_failed_independent_recall_never_calls_model(tmp_path, monkeypatch):
    def fail(self, *, query):
        raise RSITransportError("recall offline")
    monkeypatch.setattr(SyncAtoms, "recall_context", fail, raising=False)
    native = OpenAI(api_key="model")
    business = []
    native.responses.create = lambda **kwargs: business.append(kwargs)
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    try:
        with pytest.raises(RSITransportError, match="recall offline"):
            client.responses.create(model="business", input="task")
        assert business == []
    finally:
        client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("raw_mode", ["with_raw_response", "with_streaming_response"])
@pytest.mark.asyncio
async def test_native_raw_stream_recall_precedes_model_and_never_sends_run(tmp_path, monkeypatch, asynchronous, raw_mode):
    order, rsi_requests = [], []
    def recall(self, *, query):
        order.append("recall")
        return context()
    async def arecall(self, *, query):
        return recall(self, query=query)
    monkeypatch.setattr(SyncAtoms, "recall_context", recall)
    monkeypatch.setattr(AsyncAtoms, "recall_context", arecall)
    def model_server(request):
        order.append("model")
        assert request.headers["authorization"] == "Bearer model-only-key"
        assert "api-key" not in request.headers
        assert "recall memory" in json.loads(request.content)["instructions"]
        response = {"id": "response-1", "status": "completed", "model": "m", "object": "response", "created_at": 0, "parallel_tool_calls": False, "tool_choice": "auto", "tools": [], "output": [{"type": "message", "id": "msg-1", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "done", "annotations": []}]}]}
        event = json.dumps({"type": "response.completed", "response": response})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"event: response.completed\ndata: {event}\n\ndata: [DONE]\n\n")
    model_http = (httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(model_server))
    native = (AsyncOpenAI if asynchronous else OpenAI)(api_key="model-only-key", base_url="https://model.example/v1", http_client=model_http)
    client = cosens(native, mode="disable", api_key="friday-only-key", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(lambda r: (rsi_requests.append(r), httpx.Response(500))[1]))
    try:
        call = getattr(client.responses, raw_mode).create
        if asynchronous and raw_mode == "with_streaming_response":
            async with call(model="m", input="task", stream=True) as raw:
                stream = await raw.parse()
                assert (await anext(stream)).type == "response.completed"
        elif asynchronous:
            raw = await call(model="m", input="task", stream=True)
            stream = raw.parse()
            assert (await anext(stream)).type == "response.completed"
            await stream.close()
        elif raw_mode == "with_streaming_response":
            with call(model="m", input="task", stream=True) as raw:
                assert next(raw.parse()).type == "response.completed"
        else:
            raw = call(model="m", input="task", stream=True)
            stream = raw.parse()
            assert next(stream).type == "response.completed"
            stream.close()
        assert order == ["recall", "model"]
        assert rsi_requests == []
        assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


@pytest.mark.asyncio
async def test_unconsumed_async_stream_context_records_incomplete_turn(tmp_path, empty_recall):
    native = AsyncOpenAI(api_key="model", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=""))))
    client = cosens(native, mode="disable", state_path=tmp_path / "state.sqlite3")
    try:
        async with client.responses.with_streaming_response.create(model="m", input="task", stream=True):
            pass
        assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "incomplete"
    finally:
        await client.close()
