"""Private CoSens HTTP transport."""

import json

import httpx

from ._config import resolve_service
from .errors import RSIHTTPError, RSITransportError
from .models import result


def _redact(value, api_key, *sensitive_values):
    text = str(value)
    for secret in (api_key, *sensitive_values):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _decode(response, api_key, sensitive_values=()):
    def redact(value):
        return _redact(value, api_key, *sensitive_values)
    if 300 <= response.status_code < 400:
        raise RSIHTTPError(response.status_code, "CoSens gateway redirects are not followed", request_id=redact(response.headers.get("x-request-id")) if response.headers.get("x-request-id") else None)
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        if response.status_code >= 400:
            raise RSIHTTPError(response.status_code, redact(response.reason_phrase), request_id=redact(response.headers.get("x-request-id")) if response.headers.get("x-request-id") else None) from None
        raise RSITransportError(f"CoSens returned invalid JSON (HTTP {response.status_code})") from exc
    if response.status_code >= 400:
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        detail = payload.get("detail") if isinstance(payload, dict) else None
        structured_detail = detail if isinstance(detail, dict) else {}
        message = (error.get("message") if isinstance(error, dict) else None) or structured_detail.get("message")
        if not message:
            message = str(detail or payload)
        code = (error.get("code") if isinstance(error, dict) else None) or structured_detail.get("code")
        raise RSIHTTPError(
            response.status_code,
            redact(message),
            code=redact(code) if code is not None else None,
            request_id=redact(response.headers.get("x-request-id")) if response.headers.get("x-request-id") else None,
        )
    return result(payload)


def _headers(api_key, key):
    if not isinstance(api_key, str) or not api_key or any(c.isspace() for c in api_key):
        raise ValueError("CoSens api_key must be the bare Friday API Key; set api_key or RSI_API_KEY")
    headers = {"X-Auth-Type": "api_key", "Authorization": f"Bearer {api_key}"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _service_path(path):
    url = httpx.URL(path)
    if (url.scheme or url.host or not path.startswith("/") or path.startswith("//") or
            "\\" in path or url.path == "/api" or url.path.startswith("/api/")):
        raise ValueError("CoSens requires a service-relative path; absolute or external URLs are not allowed")
    return path


class AsyncTransport:
    def __init__(self, *, api_key=None, transport=None, timeout=30.0, service=None):
        self.api_key = api_key
        self.service = service or resolve_service()
        self.client = httpx.AsyncClient(base_url=self.service.url, timeout=timeout, transport=transport, verify=True, follow_redirects=False)

    async def request(self, method, path, *, body=None, key=None):
        return await self._send(method, path, key=key, json=body)

    async def request_bytes(self, method, path, *, content, sensitive_values=()):
        return await self._send(method, path, sensitive_values=sensitive_values, content_type="application/octet-stream", content=content)

    async def request_multipart(self, method, path, *, filename, mime_type, content, fields):
        data = {name: (str(value).lower() if isinstance(value, bool) else str(value)) for name, value in fields.items()}
        return await self._send(method, path, data=data, files={"file": (filename, content, mime_type)})

    async def _send(self, method, path, *, key=None, sensitive_values=(), content_type=None, **body):
        path = _service_path(path)
        headers = _headers(self.api_key, key)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = await self.client.request(method, path, headers=headers, **body)
        except httpx.HTTPError as exc:
            raise RSITransportError(_redact(exc, self.api_key, *sensitive_values)) from None
        return _decode(response, self.api_key, sensitive_values)

    async def aclose(self):
        await self.client.aclose()


class SyncTransport:
    def __init__(self, *, api_key=None, transport=None, timeout=30.0, service=None):
        self.api_key = api_key
        self.service = service or resolve_service()
        self.client = httpx.Client(base_url=self.service.url, timeout=timeout, transport=transport, verify=True, follow_redirects=False)

    def request(self, method, path, *, body=None, key=None):
        return self._send(method, path, key=key, json=body)

    def request_bytes(self, method, path, *, content, sensitive_values=()):
        return self._send(method, path, sensitive_values=sensitive_values, content_type="application/octet-stream", content=content)

    def request_multipart(self, method, path, *, filename, mime_type, content, fields):
        data = {name: (str(value).lower() if isinstance(value, bool) else str(value)) for name, value in fields.items()}
        return self._send(method, path, data=data, files={"file": (filename, content, mime_type)})

    def _send(self, method, path, *, key=None, sensitive_values=(), content_type=None, **body):
        path = _service_path(path)
        headers = _headers(self.api_key, key)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.client.request(method, path, headers=headers, **body)
        except httpx.HTTPError as exc:
            raise RSITransportError(_redact(exc, self.api_key, *sensitive_values)) from None
        return _decode(response, self.api_key, sensitive_values)

    def close(self):
        self.client.close()
