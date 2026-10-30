"""Document upload atoms. Each invocation performs at most one HTTP request."""

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit


_METADATA = {"thread_id", "uploaded_at", "source_kind", "workspace_id", "ingest_mode", "summary", "background",
             "wait_for_processing", "wait_timeout_seconds"}
_UPLOAD_PATH = re.compile(r"/v1/document/uploads/([A-Za-z0-9_-]+)\Z")


def document_metadata(filename, mime_type, options):
    for name, value in (("filename", filename), ("mime_type", mime_type)):
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError(f"document {name} must be nonempty text without control characters")
    if set(options) - _METADATA:
        raise TypeError("unsupported document metadata; user_id and graph_id are gateway-owned")
    body = {"filename": filename, "mime_type": mime_type}
    for name, value in options.items():
        if value is None:
            continue
        if name == "wait_for_processing":
            if not isinstance(value, bool):
                raise TypeError("wait_for_processing must be a boolean")
        elif name == "wait_timeout_seconds":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 3600:
                raise ValueError("wait_timeout_seconds must be an integer from 1 to 3600")
        elif not isinstance(value, str):
            raise TypeError(f"document {name} must be text")
        body[name] = value
    return body


def document_bytes(content):
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise TypeError("document content must be bytes")
    data = bytes(content)
    if not data:
        raise ValueError("document upload content must not be empty")
    return data


def upload_target(grant, content, service_url):
    if not isinstance(grant, dict) or grant.get("method") != "PUT" or grant.get("ok") is False:
        raise ValueError("upload grant must authorize PUT")
    if grant.get("status", "awaiting_upload") != "awaiting_upload":
        raise ValueError("upload grant is not awaiting_upload")
    url = grant.get("upload_url")
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url) or "\\" in url or "?" in url or "#" in url:
        raise ValueError("invalid document upload target")
    try:
        target, service = urlsplit(url), urlsplit(service_url)
        path = target.path
        if target.scheme or target.netloc:
            if (target.scheme != "https" or target.hostname != service.hostname or
                    (target.port if target.port is not None else 443) != (service.port if service.port is not None else 443) or
                    target.username is not None or target.password is not None or
                    not path.startswith(service.path.rstrip("/") + "/")):
                raise ValueError("invalid document upload target")
            path = path[len(service.path.rstrip("/")):]
        elif path.startswith(service.path.rstrip("/") + "/"):
            path = path[len(service.path.rstrip("/")):]
    except ValueError:
        raise ValueError("invalid document upload target") from None
    match = _UPLOAD_PATH.fullmatch(path)
    if not match:
        raise ValueError("invalid document upload target")
    if "expires_at" in grant:
        try:
            expires = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                raise ValueError()
        except (TypeError, AttributeError, ValueError):
            raise ValueError("document upload grant has expired or has an invalid expiry") from None
    data = document_bytes(content)
    if "max_bytes" in grant:
        maximum = grant["max_bytes"]
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1 or len(data) > maximum:
            raise ValueError("document upload exceeds max_bytes or grant limit is invalid")
    return path, match.group(1), data


def document_status_body(document_ids):
    if not isinstance(document_ids, list) or not document_ids or any(not isinstance(x, str) or not x.strip() for x in document_ids):
        raise ValueError("document_ids must be a nonempty list of document IDs")
    return {"document_ids": document_ids}


class AsyncDocumentAtoms:
    async def create_document_upload_grant(self, *, filename, mime_type, **metadata):
        return await self._http.request("POST", "/v1/document/upload-grants", body=document_metadata(filename, mime_type, metadata))

    async def upload_document_to_grant(self, grant, *, content):
        path, token, data = upload_target(grant, content, self._http.service.url)
        return await self._http.request_bytes("PUT", path, content=data, sensitive_values=(token,))

    async def upload_document(self, *, filename, mime_type, content, **metadata):
        body = document_metadata(filename, mime_type, metadata)
        data = document_bytes(content)
        return await self._http.request_multipart("POST", "/v1/document/upload", filename=body.pop("filename"),
                                                  mime_type=body.pop("mime_type"), content=data, fields=body)

    async def get_document_status(self, document_ids):
        return await self._http.request("POST", "/v1/document/status", body=document_status_body(document_ids))


class SyncDocumentAtoms:
    def create_document_upload_grant(self, *, filename, mime_type, **metadata):
        return self._http.request("POST", "/v1/document/upload-grants", body=document_metadata(filename, mime_type, metadata))

    def upload_document_to_grant(self, grant, *, content):
        path, token, data = upload_target(grant, content, self._http.service.url)
        return self._http.request_bytes("PUT", path, content=data, sensitive_values=(token,))

    def upload_document(self, *, filename, mime_type, content, **metadata):
        body = document_metadata(filename, mime_type, metadata)
        data = document_bytes(content)
        return self._http.request_multipart("POST", "/v1/document/upload", filename=body.pop("filename"),
                                            mime_type=body.pop("mime_type"), content=data, fields=body)

    def get_document_status(self, document_ids):
        return self._http.request("POST", "/v1/document/status", body=document_status_body(document_ids))
