import json

import httpx
import openai
import pytest

from cosens.local import ChatCompletionsModel, ModelUnavailable
from cosens.local.models import role_prompt

SCHEMA = {"type": "object"}


def client(handler):
    return openai.AsyncOpenAI(api_key="k", base_url="https://models.test/v1", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def completion(content, finish_reason="stop"):
    return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 1, "model": "m", "choices": [
        {"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": content}}]})


async def test_sends_one_schema_constrained_request_with_inline_context():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return completion('{"ok": true}')

    model = ChatCompletionsModel(client(handler), "m", max_completion_tokens=100, extra_body={"enable_thinking": False})
    assert await model.call(instruction="do", schema=SCHEMA, context={"role": "r", "context": {"x": "inline"}}) == {"ok": True}
    body = seen[0]
    assert len(seen) == 1 and body["model"] == "m" and body["max_completion_tokens"] == 100
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["enable_thinking"] is False
    assert "inline" in body["messages"][0]["content"] and "tools" not in body


@pytest.mark.parametrize("status,code,retryable", [
    (402, "insufficient_credits", False), (429, "upstream_unavailable", True), (500, "upstream_unavailable", True),
    (400, "upstream_rejected", False)])
async def test_maps_http_status_to_failure_code(status, code, retryable):
    model = ChatCompletionsModel(client(lambda request: httpx.Response(status, json={"error": {"message": "no"}})), "m")
    with pytest.raises(ModelUnavailable) as caught:
        await model.call(instruction="do", schema=SCHEMA, context={})
    assert (caught.value.code, caught.value.retryable, caught.value.upstream_status) == (code, retryable, status)


async def test_connection_failure_is_retryable():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(ModelUnavailable) as caught:
        await ChatCompletionsModel(client(handler), "m").call(instruction="do", schema=SCHEMA, context={})
    assert caught.value.code == "upstream_unavailable" and caught.value.retryable


async def test_invalid_json_and_cut_off_output_raise_value_error():
    with pytest.raises(ValueError):
        await ChatCompletionsModel(client(lambda r: completion("not json")), "m").call(instruction="d", schema=SCHEMA, context={})
    with pytest.raises(ValueError, match="max_output_tokens"):
        await ChatCompletionsModel(client(lambda r: completion('{"a"', "length")), "m").call(instruction="d", schema=SCHEMA, context={})


def test_requires_an_async_client():
    with pytest.raises(TypeError):
        ChatCompletionsModel(openai.OpenAI(api_key="k"), "m")


def test_prompt_carries_instruction_schema_and_context():
    prompt = role_prompt("do", SCHEMA, {"role": "r"})
    payload = json.loads(prompt.split("\n", 1)[1])
    assert payload == {"role": "r", "instruction": "do", "response_schema": SCHEMA}
    assert "Do not use tools" in prompt
