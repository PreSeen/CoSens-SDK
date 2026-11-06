"""Structured internal decisions through the developer's native client.

Decisions use Chat Completions with a JSON schema response format, which
OpenAI-compatible model gateways support, so no provider URL is special-cased.
"""
from types import SimpleNamespace


def _chat_options(options):
    schema = options["text"]["format"]
    body = {"model": options["model"], "messages": [{"role": "user", "content": options["input"]}],
            "response_format": {"type": "json_schema", "json_schema": {key: schema[key] for key in ("name", "strict", "schema")}}}
    if "max_output_tokens" in options:
        body["max_completion_tokens"] = options["max_output_tokens"]
    if "timeout" in options:
        body["timeout"] = options["timeout"]
    return body


def structured_sync(native, **options):
    response = native.chat.completions.create(**_chat_options(options))
    return SimpleNamespace(output_text=response.choices[0].message.content)


async def structured_async(native, **options):
    response = await native.chat.completions.create(**_chat_options(options))
    return SimpleNamespace(output_text=response.choices[0].message.content)
