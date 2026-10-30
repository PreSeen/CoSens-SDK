"""15 request-boundary regressions; real SDK code with an in-process HTTP peer."""
import asyncio
import inspect
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError, RSITransportError

pytestmark = pytest.mark.asyncio
API_KEY = "regression-api-key"
MODES = (False, True)
GRANT = {"method": "PUT", "upload_url": "/v1/document/uploads/regression_token"}


@asynccontextmanager
async def sdk(asynchronous, *, status=200, payload=None, raw=None, headers=None):
    seen = []

    def server(request):
        seen.append(request)
        if raw is not None:
            return httpx.Response(status, content=raw, headers=headers)
        return httpx.Response(status, json={} if payload is None else payload, headers=headers)

    async with CoSensClient(api_key=API_KEY, _http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms

        async def call(name, *args, **kwargs):
            result = getattr(atoms, name)(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        yield call, seen


async def test_r01_max_length_key_and_manual_replay_keep_identical_request():
    for mode in MODES:
        async with sdk(mode, payload={"task_id": "task-1"}) as (call, seen):
            for _ in range(2):
                result = await call("create_task", source="new_task", objective="整理资料", key="k" * 256)
                assert result["task_id"] == "task-1"
            assert len(seen) == 2
            assert seen[0].content == seen[1].content
            assert json.loads(seen[0].content) == {"source": "new_task", "objective": "整理资料", "artifacts": []}
            assert all(r.headers["idempotency-key"] == "k" * 256 for r in seen)


async def test_r02_oversized_key_is_rejected_before_sending():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            with pytest.raises(ValueError, match="key"):
                await call("start_execution", "task-1", key="k" * 257)
            assert not seen


async def test_r03_explicit_empty_optional_key_is_not_silently_omitted():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            methods = ["submit"] if not mode else ["submit", "feedback", "improve"]
            for method in methods:
                options = {"reason": "revise"} if method == "improve" else {"data": {}}
                with pytest.raises(ValueError, match="key"):
                    await call(method, "run-1", key="", **options)
            assert not seen


async def test_r04_control_characters_in_keys_are_rejected_before_sending():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            for key in ("safe\r\nX-Injected: yes", "nul\x00key", "del\x7fkey"):
                with pytest.raises(ValueError, match="key"):
                    await call("start_execution", "task-1", key=key)
            assert not seen


async def test_r05_memory_request_key_stays_in_json_and_preserves_false_and_zero():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            await call("memory_ingest", content="test memory", request_key="source-key", source_kind="message", durable=False, expected_revision=0)
            assert len(seen) == 1
            assert "idempotency-key" not in seen[0].headers
            assert json.loads(seen[0].content) == {"content": "test memory", "request_key": "source-key", "source_kind": "message", "durable": False, "expected_revision": 0}


async def test_r06_key_does_not_leak_into_following_read_request():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            await call("start_execution", "task-1", key="execution-key")
            await call("get_task", "task-1")
            await call("submit", "run-1")
            assert len(seen) == 3
            assert seen[0].headers["idempotency-key"] == "execution-key"
            assert all("idempotency-key" not in r.headers for r in seen[1:])
            assert all(r.headers["authorization"] == f"Bearer {API_KEY}" and r.headers["x-auth-type"] == "api_key" for r in seen)


async def test_r07_conflict_preserves_diagnostics_and_is_not_retried():
    for mode in MODES:
        async with sdk(mode, status=409, payload={"error": {"code": "idempotency_conflict", "message": "request body changed"}}, headers={"x-request-id": "request-409"}) as (call, seen):
            with pytest.raises(RSIHTTPError) as caught:
                await call("start_execution", "task-1", key="conflict-key")
            error = caught.value
            assert (error.status_code, error.code, error.request_id, str(error)) == (409, "idempotency_conflict", "request-409", "request body changed")
            assert len(seen) == 1
        async with sdk(mode, status=401, payload={"detail": {"code": "unauthenticated", "message": "invalid bearer credential"}}, headers={"x-request-id": "request-401"}) as (call, seen):
            with pytest.raises(RSIHTTPError) as caught:
                await call("get_task", "task-1")
            error = caught.value
            assert (error.status_code, error.code, error.request_id, str(error)) == (401, "unauthenticated", "request-401", "invalid bearer credential")
            assert len(seen) == 1
        async with sdk(mode, status=401, payload={"detail": {"code": API_KEY, "message": f"bad {API_KEY}"}}, headers={"x-request-id": API_KEY}) as (call, seen):
            with pytest.raises(RSIHTTPError) as caught:
                await call("get_task", "task-1")
            error = caught.value
            assert API_KEY not in str(error) and API_KEY not in error.code and API_KEY not in error.request_id
            assert len(seen) == 1


async def test_r08_rate_limit_does_not_trigger_automatic_mutation_retry():
    for mode in MODES:
        async with sdk(mode, status=429, payload={"detail": "rate limited"}, headers={"retry-after": "0"}) as (call, seen):
            with pytest.raises(RSIHTTPError) as caught:
                await call("start_execution", "task-1", key="rate-key")
            assert caught.value.status_code == 429
            assert len(seen) == 1


async def test_r09_malformed_success_body_reports_uncertain_upload_without_retry():
    for mode in MODES:
        async with sdk(mode, raw=b"{not-json") as (call, seen):
            with pytest.raises(RSITransportError, match="invalid JSON"):
                await call("upload_document_to_grant", GRANT, content=b"payload")
            assert len(seen) == 1 and seen[0].method == "PUT"


async def test_r10_non_json_gateway_error_keeps_status_and_request_id():
    for mode in MODES:
        async with sdk(mode, status=502, raw=b"<html>bad gateway</html>", headers={"x-request-id": "proxy-502"}) as (call, seen):
            with pytest.raises(RSIHTTPError) as caught:
                await call("get_task", "task-1")
            assert (caught.value.status_code, caught.value.request_id) == (502, "proxy-502")
            assert len(seen) == 1


async def test_r11_concurrent_requests_keep_their_own_keys_and_bodies():
    seen = []
    both_entered = asyncio.Event()

    async def server(request):
        seen.append(request)
        if len(seen) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        return httpx.Response(200, json={"objective": json.loads(request.content)["objective"]})

    async with CoSensClient(api_key=API_KEY, _http_transport=httpx.MockTransport(server)) as client:
        results = await asyncio.gather(
            client.atoms.create_task(source="new_task", objective="alpha", key="key-alpha"),
            client.atoms.create_task(source="new_task", objective="beta", key="key-beta"),
        )
    assert [r["objective"] for r in results] == ["alpha", "beta"]
    assert sorted((r.headers["idempotency-key"], json.loads(r.content)["objective"]) for r in seen) == [("key-alpha", "alpha"), ("key-beta", "beta")]


async def test_r12_upload_exact_byte_limit_and_timezone_offset_are_accepted():
    for mode in MODES:
        async with sdk(mode, payload={"document_id": "doc-1"}) as (call, seen):
            content = "中文".encode("utf-8")
            for index, expiry in enumerate(("2099-01-01T08:00:00+08:00", "2104-02-29T00:00:00Z", "2400-02-29T00:00:00.123456Z")):
                grant = {"method": "PUT", "upload_url": f"https://friday-rsi-cn.stardust-ai.com:443/api/v1/document/uploads/token_{index}", "expires_at": expiry, "max_bytes": 6}
                receipt = await call("upload_document_to_grant", grant, content=content)
                assert receipt["document_id"] == "doc-1"
            assert len(seen) == 3 and all(r.content == b"\xe4\xb8\xad\xe6\x96\x87" for r in seen)


async def test_r13_impossible_calendar_expiry_is_rejected_before_consuming_token():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            for expiry in ("2099-02-30T00:00:00Z", "2100-02-29T00:00:00Z", "2099-04-31T00:00:00+08:00", "2099-01-01T24:00:00Z", "2099-01-01T00:00:00"):
                with pytest.raises(ValueError, match="expiry"):
                    await call("upload_document_to_grant", {**GRANT, "expires_at": expiry}, content=b"x")
            assert not seen


async def test_r14_reserved_characters_in_resource_id_remain_one_encoded_segment():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            await call("get_task", "a/b?c#d%2F")
            assert len(seen) == 1
            assert seen[0].url.raw_path == b"/api/v1/tasks/a%2Fb%3Fc%23d%252F"


async def test_r15_upload_port_zero_is_not_treated_as_default_https_port():
    for mode in MODES:
        async with sdk(mode) as (call, seen):
            with pytest.raises(ValueError, match="upload target"):
                await call("upload_document_to_grant", {**GRANT, "upload_url": "https://friday-rsi-cn.stardust-ai.com:0/api/v1/document/uploads/regression_token"}, content=b"x")
            assert not seen
