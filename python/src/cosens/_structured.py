"""Structured internal decisions using each gateway's declared contract."""
from types import SimpleNamespace

_GATEWAYS = {"https://llm.cosens.ai/v1", "https://friday-llm-gateway.corpintra.rosettalab.top/v1"}


def _chat_options(native, options):
    if str(getattr(native, "base_url", "")).rstrip("/") not in _GATEWAYS:
        return None
    schema = options["text"]["format"]
    body = {"model": options["model"], "messages": [{"role": "user", "content": options["input"]}],
            "response_format": {"type": "json_schema", "json_schema": {key: schema[key] for key in ("name", "strict", "schema")}}}
    if "max_output_tokens" in options:
        body["max_completion_tokens"] = options["max_output_tokens"]
    if "timeout" in options:
        body["timeout"] = options["timeout"]
    return body


def structured_sync(native, **options):
    body = _chat_options(native, options)
    if body is None:
        return native.responses.create(**options)
    response = native.chat.completions.create(**body)
    return SimpleNamespace(output_text=response.choices[0].message.content)


async def structured_async(native, **options):
    body = _chat_options(native, options)
    if body is None:
        return await native.responses.create(**options)
    response = await native.chat.completions.create(**body)
    return SimpleNamespace(output_text=response.choices[0].message.content)
