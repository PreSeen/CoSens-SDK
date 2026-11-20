"""Atomic asynchronous operations for the CoSens Memory service."""

import os
from urllib.parse import quote, urlencode

import httpx

_SERVICE_URL = "https://rsi.cosens.ai/api/v1/memory"


class MemoryError(RuntimeError):
    def __init__(self, message, *, status=None, code=None, request_id=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id


def _bounded_integer(value, name, minimum, maximum):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _path_part(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("resource ID must be a nonempty safe path segment")
    return quote(value, safe="")


async def memory_recall(*, query, token_budget=1000, virtual_user_id=None):
    return await _request(
        "POST",
        "/recall",
        {
            "query": query,
            "token_budget": _bounded_integer(token_budget, "token_budget", 1, 8000),
        },
        virtual_user_id=virtual_user_id,
    )


async def memory_ingest(
    *,
    content,
    request_key,
    source_kind="decision",
    durable=True,
    source_id=None,
    expected_revision=None,
    virtual_user_id=None,
):
    if (
        not isinstance(request_key, str)
        or not request_key.strip()
        or len(request_key) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in request_key)
    ):
        raise ValueError(
            "request_key must be 1–256 characters without control characters"
        )
    body = {
        "content": content,
        "request_key": request_key,
        "source_kind": source_kind,
        "durable": durable,
    }
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a nonempty string")
        body["source_id"] = source_id
    if expected_revision is not None:
        body["expected_revision"] = _bounded_integer(
            expected_revision, "expected_revision", 0, 2**53 - 1
        )
    return await _request("POST", "/ingest", body, virtual_user_id=virtual_user_id)


async def get_memory_items(*, limit=200, virtual_user_id=None):
    limit = _bounded_integer(limit, "limit", 1, 500)
    return await _request("GET", f"/items?limit={limit}", virtual_user_id=virtual_user_id)


async def get_memory_job(job_id, *, virtual_user_id=None):
    return await _request("GET", f"/jobs/{_path_part(job_id)}", virtual_user_id=virtual_user_id)


async def get_memory_experience(experience_id, *, revision=None, virtual_user_id=None):
    if virtual_user_id is not None:
        raise ValueError("Virtual User selection is not supported for experience details")
    suffix = "?" + urlencode({"revision": revision}) if revision is not None else ""
    return await _request("GET", f"/experiences/{_path_part(experience_id)}{suffix}")


async def get_memory(*, virtual_user_id=None):
    if virtual_user_id is not None:
        raise ValueError("Virtual User selection is not supported for memory overview")
    return await _request("GET", "")


async def _request(method, path, body=None, *, virtual_user_id=None):
    if virtual_user_id is not None and (not isinstance(virtual_user_id, str) or not virtual_user_id or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in virtual_user_id)):
        raise ValueError("virtual_user_id must be a nonempty header value")
    api_key = os.getenv("COSENS_API_KEY")
    if not api_key or any(c.isspace() for c in api_key):
        raise ValueError("Set COSENS_API_KEY to the bare CoSens API key")

    def redact(value):
        return str(value).replace(api_key, "[REDACTED]")

    try:
        async with httpx.AsyncClient(
            headers={"X-Auth-Type": "api_key", "Authorization": f"Bearer {api_key}", **({"X-Virtual-User-Id": virtual_user_id} if virtual_user_id is not None else {})},
            follow_redirects=False,
            timeout=30.0,
        ) as client:
            response = await client.request(method, _SERVICE_URL + path, json=body)
    except httpx.HTTPError:
        raise MemoryError("CoSens Memory request failed") from None

    status = response.status_code
    header = response.headers.get("x-request-id")
    request_id = redact(header) if header else None
    if 300 <= status < 400:
        raise MemoryError(
            "CoSens gateway redirects are not followed",
            status=status,
            request_id=request_id,
        )
    try:
        payload = response.json()
    except ValueError:
        message = (
            "CoSens Memory returned invalid JSON"
            if response.is_success
            else response.reason_phrase
        )
        raise MemoryError(
            redact(message), status=status, request_id=request_id
        ) from None

    if not response.is_success:
        payload = payload if isinstance(payload, dict) else {}
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        detail = payload.get("detail")
        structured = detail if isinstance(detail, dict) else {}
        message = (
            error.get("message")
            or structured.get("message")
            or detail
            or response.reason_phrase
        )
        code = error.get("code", structured.get("code"))
        raise MemoryError(
            redact(message),
            status=status,
            code=redact(code) if code is not None else None,
            request_id=request_id,
        )
    return payload
