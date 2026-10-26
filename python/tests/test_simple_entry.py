import json

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from cosens.errors import RSIDecisionUnavailable


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("api", ["responses", "chat"])
@pytest.mark.asyncio
async def test_three_argument_entry_uses_request_model_and_captures_without_session(tmp_path, asynchronous, api):
    model_requests, gateway_requests = [], []

    def model_server(request):
        body = json.loads(request.content)
        model_requests.append((request, body))
        if body.get("text", {}).get("format", {}).get("name") == "cosens_decision":
            text = json.dumps({"activate": True, "objective": "hello", "task_family": "general", "reason": "useful"})
        else:
            text = "done"
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"id": "chat-1", "object": "chat.completion", "created": 0, "model": "business-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]})
        return httpx.Response(200, json={"id": "resp-1", "object": "response", "created_at": 0, "model": "business-model", "status": "completed", "output": [{"id": "msg-1", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}]})

    def gateway_server(request):
        body = json.loads(request.content) if request.content else None
        gateway_requests.append((request, body))
        if request.url.path.endswith("/prepare"):
            return httpx.Response(200, json={"run_id": "r1", "status": "ready", "recipe": {}, "context": {}})
        if request.url.path.endswith("/session-events"):
            return httpx.Response(200, json={"durable_ack": [e["id"] for e in body["events"]]})
        if request.url.path.endswith("/submit"):
            return httpx.Response(200, json={"status": "completed"})
        return httpx.Response(200, json={"run_id": "r1", "status": "ready", "snapshot": {}})

    http = httpx.AsyncClient if asynchronous else httpx.Client
    constructor = AsyncOpenAI if asynchronous else OpenAI
    native = constructor(api_key="model-key", http_client=http(transport=httpx.MockTransport(model_server)))
    client = cosens(native, api_key="friday-key", mode="auto", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(gateway_server))
    try:
        method = client.responses.create if api == "responses" else client.chat.completions.create
        kwargs = {"input": "hello"} if api == "responses" else {"messages": [{"role": "user", "content": "hello"}]}
        response = await method(model="business-model", **kwargs) if asynchronous else method(model="business-model", **kwargs)
        assert (response.output_text if api == "responses" else response.choices[0].message.content) == "done"
        assert [body["model"] for _, body in model_requests] == ["business-model", "business-model"]
        assert model_requests[0][1]["store"] is False
        assert all(request.headers["authorization"] == "Bearer model-key" for request, _ in model_requests)
        assert all(request.headers["authorization"] == "Bearer friday-key" for request, _ in gateway_requests)
        events = [e for request, body in gateway_requests if request.url.path.endswith("/session-events") for e in body["events"]]
        assert [(e["payload"]["role"], e["payload"]["content"]) for e in events] == [("user", "hello"), ("assistant", "done")]
        assert client.cosens.get_turn(client.cosens.last_turn_id)["state"] == "completed"
    finally:
        if asynchronous:
            await client.close()
        else:
            client.close()


def test_missing_request_model_has_clear_error_before_network(tmp_path):
    requests = []
    native = OpenAI(api_key="model-key", http_client=httpx.Client(transport=httpx.MockTransport(lambda r: requests.append(r))))
    client = cosens(native, api_key="friday-key", mode="auto", state_path=tmp_path / "state.sqlite3")
    try:
        with pytest.raises(RSIDecisionUnavailable, match="model"):
            client.responses.create(input="hello")
        assert requests == []
    finally:
        client.close()
