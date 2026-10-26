"""Private CoSens HTTP transport."""

import json

import httpx

from ._config import SERVICE_URL
from .errors import RSIHTTPError, RSITransportError
from .models import result


def _decode(response):
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise RSITransportError(f"CoSens returned invalid JSON (HTTP {response.status_code})") from exc
    if response.status_code >= 400:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        detail = payload.get("detail") if isinstance(payload, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if not message:
            message = str(detail or payload)
        raise RSIHTTPError(
            response.status_code,
            message,
            code=error.get("code") if isinstance(error, dict) else None,
            request_id=response.headers.get("x-request-id"),
        )
    return result(payload)


class AsyncTransport:
    def __init__(self, token: str | None, *, transport=None, timeout=30.0):
        self.token = token
        self.client = httpx.AsyncClient(base_url=SERVICE_URL, timeout=timeout, transport=transport)

    async def request(self, method, path, *, body=None, key=None):
        if not self.token:
            raise ValueError("A CoSens bearer token is required")
        headers = {"Authorization": f"Bearer {self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        try:
            response = await self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RSITransportError(str(exc)) from exc
        return _decode(response)

    async def aclose(self):
        await self.client.aclose()
