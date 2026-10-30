import inspect
import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError, RSITransportError


GRANT = {"status": "awaiting_upload", "method": "PUT",
         "upload_url": "https://friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/test_token",
         "expires_at": "2099-01-01T00:00:00Z", "max_bytes": 20}


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_document_grant_put_and_status_preserve_bytes_and_gateway_identity(asynchronous):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json=GRANT if request.url.path.endswith("upload-grants") else {"document_id": "doc-1", "status": "processing"})
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(server)) as c:
        atoms = c.atoms if asynchronous else c.sync_atoms
        async def call(name, *args, **kwargs):
            value = getattr(atoms, name)(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value
        grant = await call("create_document_upload_grant", filename="sample.bin", mime_type="application/octet-stream", thread_id="thread-1", wait_for_processing=False)
        receipt = await call("upload_document_to_grant", grant, content=b"\x00\xffbinary\r\n")
        assert receipt.document_id == "doc-1" and receipt.status == "processing"
        await call("get_document_status", [receipt.document_id])
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/api/v1/document/upload-grants"), ("PUT", "/api/v1/document/uploads/test_token"), ("POST", "/api/v1/document/status")]
    assert json.loads(seen[0].content) == {"filename": "sample.bin", "mime_type": "application/octet-stream", "thread_id": "thread-1", "wait_for_processing": False}
    assert seen[1].content == b"\x00\xffbinary\r\n"
    assert seen[1].headers["content-type"] == "application/octet-stream"
    assert json.loads(seen[2].content) == {"document_ids": ["doc-1"]}
    assert all(r.headers["authorization"] == "Bearer document-key" and r.headers["x-auth-type"] == "api_key" and "x-user-id" not in r.headers for r in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_document_multipart_has_file_bytes_and_explicit_metadata(asynchronous):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"document_id": "doc-1"})
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(server)) as c:
        atoms = c.atoms if asynchronous else c.sync_atoms
        value = atoms.upload_document(filename="sample.txt", mime_type="text/plain", content=b"hello\x00world", ingest_mode="fulltext", wait_for_processing=False)
        if asynchronous: await value
    request = seen[0]
    assert len(seen) == 1 and request.url.path == "/api/v1/document/upload"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="sample.txt"' in request.content and b"hello\x00world" in request.content
    assert b'name="ingest_mode"\r\n\r\nfulltext' in request.content
    assert b'name="wait_for_processing"\r\n\r\nfalse' in request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://attacker.example/api/v1/document/uploads/token",
    "https://rsi.cosens.ai/api/v1/document/uploads/token",
    "http://friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token",
    "https://user:password@friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token",
    "https://@friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token",
    "https://friday-rsi-cn.stardust-ai.com:444/api/v1/document/uploads/token",
    "/v1/document/uploads/../token", "/v1/document/uploads/%2e%2e", "/v1/document/uploads/token?x=y",
    "/v1/document/uploads/token#fragment", "/v1/document/uploads/a/b", "/v1/other/token",
    "//attacker.example/v1/document/uploads/token",
])
async def test_upload_grant_rejects_unsafe_targets_before_authentication(url):
    seen = []
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(lambda r: seen.append(r))) as c:
        with pytest.raises((TypeError, ValueError)):
            await c.atoms.upload_document_to_grant({**GRANT, "upload_url": url}, content=b"data")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("grant,content", [
    ({**GRANT, "method": "POST"}, b"x"), ({**GRANT, "expires_at": "2000-01-01T00:00:00Z"}, b"x"),
    ({**GRANT, "max_bytes": 1}, b"large"), (GRANT, b""), (GRANT, "not bytes"),
])
async def test_grant_preflight_does_not_consume_token_on_invalid_input(grant, content):
    seen = []
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(lambda r: seen.append(r))) as c:
        with pytest.raises((TypeError, ValueError)):
            await c.atoms.upload_document_to_grant(grant, content=content)
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redirect", "network", "http"])
async def test_one_time_upload_never_retries_or_exposes_credentials(failure):
    seen = []
    def server(request):
        seen.append(request)
        if failure == "network": raise httpx.ReadTimeout("test_token document-key", request=request)
        if failure == "redirect": return httpx.Response(307, headers={"location": "https://attacker.example/upload"})
        return httpx.Response(500, json={"error": {"message": "test_token document-key"}})
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(server)) as c:
        with pytest.raises((RSIHTTPError, RSITransportError)) as exc:
            await c.atoms.upload_document_to_grant(GRANT, content=b"data")
        assert "document-key" not in str(exc.value) and "test_token" not in str(exc.value)
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("url", ["/v1/document/uploads/test_token", "/api/v1/document/uploads/test_token"])
async def test_relative_grants_preserve_gateway_prefix_and_ignore_response_instructions(asynchronous, url):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": False, "status": "failed", "failure_kind": "unsupported_format"})
    grant = {"upload_url": url, "method": "PUT", "headers": {"Authorization": "untrusted"}, "curl": "untrusted instruction"}
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(server)) as c:
        value = (c.atoms if asynchronous else c.sync_atoms).upload_document_to_grant(grant, content=b"data")
        receipt = await value if asynchronous else value
    assert receipt["ok"] is False and receipt["status"] == "failed"
    assert len(seen) == 1 and seen[0].url.path == "/api/v1/document/uploads/test_token"
    assert seen[0].headers["authorization"] == "Bearer document-key"


@pytest.mark.asyncio
async def test_document_metadata_cannot_override_gateway_identity():
    seen = []
    async with CoSensClient(api_key="document-key", _http_transport=httpx.MockTransport(lambda r: seen.append(r))) as c:
        for extra in ({"user_id": "other"}, {"graph_id": "other"}, {"wait_timeout_seconds": 0}, {"wait_for_processing": "false"}):
            with pytest.raises((TypeError, ValueError)):
                await c.atoms.create_document_upload_grant(filename="a.txt", mime_type="text/plain", **extra)
        with pytest.raises(ValueError): await c.atoms.get_document_status([])
    assert seen == []
