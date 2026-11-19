"""Wire protocol for the model calls made by an RSI node."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, Field, ValidationError

from .models import ModelOutputError, RSIError


def _user_agent():
    try:
        release = _package_version("cosens-sdk")
    except PackageNotFoundError:
        release = "source"
    return f"cosens-sdk-python/{release}"


USER_AGENT = _user_agent()


class ModelStatusError(Exception):
    def __init__(self, status_code):
        super().__init__("Model endpoint rejected the request")
        self.status_code = status_code


class ModelTimeout(RSIError):
    code = "timeout"


class ModelUnavailable(RSIError):
    code = "model_unavailable"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FunctionCall(BaseModel):
    name: str = Field(min_length=1)
    arguments: str


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: FunctionCall


class Assistant(BaseModel):
    role: Literal["assistant"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    reasoning_content: str | None = None


class Choice(BaseModel):
    message: Assistant
    finish_reason: str | None = None


class Completion(BaseModel):
    choices: list[Choice] = Field(min_length=1, max_length=1)


def function_definition(name, description, schema):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema.model_json_schema(),
        },
    }


@dataclass
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable

    def definition(self):
        return function_definition(self.name, self.description, self.schema)

    def invoke(self, arguments):
        args = self.schema.model_validate(arguments)
        return self.handler(**args.model_dump())


def request_completion(config, key, messages, tools):
    base = config.base_url or "https://api.openai.com/v1"
    request = Request(
        base.rstrip("/") + "/chat/completions",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
        data=json.dumps(
            {
                "model": config.model,
                **(
                    {"max_tokens": config.max_tokens}
                    if config.max_tokens is not None
                    else {}
                ),
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            },
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    try:
        with build_opener(NoRedirect()).open(
            request, timeout=config.timeout
        ) as response:
            raw = response.read()
    except HTTPError as error:
        error.close()
        raise ModelStatusError(error.code) from None
    except TimeoutError:
        raise ModelTimeout("Model request timed out") from None
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise ModelTimeout("Model request timed out") from None
        raise ModelUnavailable("Model request could not be completed") from None
    except (OSError, HTTPException):
        raise ModelUnavailable("Model request could not be completed") from None
    try:
        choice = Completion.model_validate_json(raw).choices[0]
    except ValidationError:
        raise ModelOutputError("Invalid model response envelope") from None
    if choice.finish_reason in {"length", "content_filter"}:
        raise ModelOutputError("Model response ended before completion")
    ids = [call.id for call in choice.message.tool_calls or []]
    if len(set(ids)) != len(ids):
        raise ModelOutputError("Model repeated tool call IDs in one response")
    return choice.message
