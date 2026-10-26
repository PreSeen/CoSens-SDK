"""Private CoSens HTTP transport."""

import json

import httpx

from ._config import SERVICE_URL
from .errors import RSIHTTPError, RSITransportError
from .models import result


def _decode(response):
    if 300 <= response.status_code < 400:
        raise RSIHTTPError(response.status_code, "CoSens gateway redirects are not followed", request_id=response.headers.get("x-request-id"))
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        if response.status_code >= 400:
            raise RSIHTTPError(response.status_code, response.reason_phrase, request_id=response.headers.get("x-request-id")) from exc
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


def _headers(api_key, key):
    if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
        raise ValueError("CoSens api_key must be the bare Friday API Key; set api_key or RSI_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _service_path(path):
    url = httpx.URL(path)
    if url.scheme or url.host or not path.startswith("/") or path.startswith("//"):
        raise ValueError("CoSens requires a service-relative path; absolute or external URLs are not allowed")
    return path


class AsyncTransport:
    def __init__(self, *, api_key=None, transport=None, timeout=30.0):
        self.api_key = api_key
        self.client = httpx.AsyncClient(base_url=SERVICE_URL, timeout=timeout, transport=transport, verify=True, follow_redirects=False)

    async def request(self, method, path, *, body=None, key=None):
        path = _service_path(path)
        headers = _headers(self.api_key, key)
        try:
            response = await self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RSITransportError(str(exc)) from exc
        return _decode(response)

    async def aclose(self):
        await self.client.aclose()


class SyncTransport:
    def __init__(self, *, api_key=None, transport=None, timeout=30.0):
        self.api_key = api_key
        self.client = httpx.Client(base_url=SERVICE_URL, timeout=timeout, transport=transport, verify=True, follow_redirects=False)

    def request(self, method, path, *, body=None, key=None):
        path = _service_path(path)
        headers = _headers(self.api_key, key)
        try:
            response = self.client.request(method, path, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RSITransportError(str(exc)) from exc
        return _decode(response)

    def close(self):
        self.client.close()
