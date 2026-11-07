"""Structured internal decisions through the developer's native client.

Decisions use Chat Completions with a JSON schema response format, which
OpenAI-compatible model gateways support, so no provider URL is special-cased.
``extra_body`` (for example ``{"enable_thinking": False}``) is forwarded unchanged.
The caller passes a client that does not retry (see ``_Runtime._policy_client``).
"""
from types import SimpleNamespace

from .errors import RSIDecisionUnavailable


def _chat_options(options):
    schema = options["text"]["format"]
    body = {"model": options["model"], "messages": [{"role": "user", "content": options["input"]}],
            "response_format": {"type": "json_schema", "json_schema": {key: schema[key] for key in ("name", "strict", "schema")}}}
    if "max_output_tokens" in options:
        body["max_completion_tokens"] = options["max_output_tokens"]
    if "timeout" in options:
        body["timeout"] = options["timeout"]
    if options.get("extra_body"):
        body["extra_body"] = options["extra_body"]
    return body


def _output(response, options):
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        # Reasoning models can spend the whole budget before writing the JSON.
        raise RSIDecisionUnavailable(
            f"Policy output was cut off at max_output_tokens={options.get('max_output_tokens')} (finish_reason=length); "
            "disable reasoning for the decision or use a non-reasoning decision_client")
    return SimpleNamespace(output_text=choice.message.content)


def structured_sync(native, **options):
    return _output(native.chat.completions.create(**_chat_options(options)), options)


async def structured_async(native, **options):
    return _output(await native.chat.completions.create(**_chat_options(options)), options)
