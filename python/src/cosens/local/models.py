"""The one interface local RSI uses to reach a model, and the developer-supplied client implementation."""

import asyncio
import json
from typing import Protocol

import httpx
import openai

from ..workflow_atoms import _completion_tokens

PROMPT_PREAMBLE = (
    "Act ONLY as the top-level role in the JSON below and follow its instruction. "
    "Its evidence describes recorded work for your role, NOT a request to execute that work. "
    "Do not return that task's answer or deliverables instead of the role's required JSON schema. "
    "Use only the supplied evidence. Do not use tools or read files. "
    "Answer with the role's requested JSON object only.\n"
)


class ModelUnavailable(RuntimeError):
    """The model did not answer. ``code`` and ``retryable`` follow the service's failure codes."""

    def __init__(self, message, *, code="upstream_unavailable", retryable=True, upstream_status=None, upstream_code=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code


class RecipeModel(Protocol):
    identity: str
    timeout: float

    async def call(self, *, instruction: str, schema: dict, context: dict) -> dict:
        """Run one request with the whole context inline and return the JSON object."""


def role_prompt(instruction, schema, context):
    return PROMPT_PREAMBLE + json.dumps({**context, "instruction": instruction, "response_schema": schema},
                                        ensure_ascii=False, allow_nan=False)


def parse_object(text):
    if not isinstance(text, str):
        raise ValueError("the final response was not valid JSON")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("the final response was not a JSON object")
    return value


def positive_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    return value


def _status_failure(exc):
    status = exc.status_code
    details = {"upstream_status": status, "upstream_code": getattr(exc, "code", None)}
    if status == 402:
        return ModelUnavailable("The model provider reported insufficient credits.", code="insufficient_credits",
                                retryable=False, **details)
    if status in (408, 429) or status >= 500:
        return ModelUnavailable("The model call did not complete; resume to retry.", **details)
    return ModelUnavailable("The model provider rejected the request.", code="upstream_rejected", retryable=False, **details)


class ChatCompletionsModel:
    """Calls a developer-built ``AsyncOpenAI`` client with one schema-constrained, streamed request per stage.

    The response is streamed because proxies cut a request that sends no bytes for about two minutes. Retries are off so
    one call stays within ``timeout``; a failed stage is retried with ``resume_recipe_candidate``.
    """

    def __init__(self, client, model, *, max_completion_tokens=32000, timeout=300, extra_body=None):
        if not isinstance(client, openai.AsyncOpenAI):
            raise TypeError("client must be an AsyncOpenAI client")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a nonempty model ID")
        self._client = client.with_options(max_retries=0)
        self._model = model.strip()
        self._max_completion_tokens = _completion_tokens(max_completion_tokens)
        self._extra_body = extra_body
        self.timeout = positive_timeout(timeout)
        self.identity = self._model

    async def _streamed_text(self, prompt, schema):
        stream = await self._client.chat.completions.create(
            model=self._model, messages=[{"role": "user", "content": prompt}], stream=True, timeout=self.timeout,
            response_format={"type": "json_schema", "json_schema": {"name": "recipe_output", "strict": True, "schema": schema}},
            max_completion_tokens=self._max_completion_tokens,
            **({"extra_body": self._extra_body} if self._extra_body else {}),
        )
        parts, finish_reason = [], None
        async with stream:
            async for chunk in stream:
                if chunk.choices:
                    choice = chunk.choices[0]
                    if choice.delta is not None and choice.delta.content:
                        parts.append(choice.delta.content)
                    finish_reason = choice.finish_reason or finish_reason
        return "".join(parts), finish_reason

    async def call(self, *, instruction, schema, context):
        prompt = role_prompt(instruction, schema, context)
        try:
            text, finish_reason = await asyncio.wait_for(self._streamed_text(prompt, schema), self.timeout)
        except asyncio.TimeoutError as exc:
            raise ModelUnavailable(f"The model did not answer within {self.timeout} seconds.") from exc
        except openai.APIStatusError as exc:
            raise _status_failure(exc) from exc
        except (openai.APIError, httpx.HTTPError) as exc:  # connection failures and streams cut part way
            raise ModelUnavailable("The model call did not complete; resume to retry.") from exc
        if finish_reason is None:
            raise ModelUnavailable("The model stream ended before the response was complete; resume to retry.")
        if finish_reason == "length":
            raise ValueError(f"the response was cut off at max_completion_tokens={self._max_completion_tokens}")
        return parse_object(text)
