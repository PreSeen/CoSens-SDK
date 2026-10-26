import httpx
import pytest

from cosens import CoSensClient
from cosens import cosens
from cosens.errors import RSIHTTPError
from cosens.http import AsyncTransport, SyncTransport
from cosens.state import TurnStore


@pytest.mark.asyncio
async def test_gateway_retains_service_paths_queries_and_headers_for_both_transports():
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ready", "run_id": "r1"})

    sync = SyncTransport("ordinary", api_key="test-gateway", transport=httpx.MockTransport(server))
    async_transport = AsyncTransport("ordinary", api_key="test-gateway", transport=httpx.MockTransport(server))
    try:
        sync.request("GET", "/v1/runs/r1?include_context=true")
        await async_transport.request("POST", "/v1/runs/prepare", body={"objective": "task"}, key="turn-key")
        async with CoSensClient(api_key="test-gateway", token="ordinary", admin_token="admin", _http_transport=httpx.MockTransport(server)) as client:
            await client.admin.atoms.publish("j1", expected_revision=2, reason="verified")
    finally:
        sync.close()
        await async_transport.aclose()

    assert [str(r.url) for r in requests] == [
        "https://friday-rsi-cn.stardust-ai.com/api/v1/runs/r1?include_context=true",
        "https://friday-rsi-cn.stardust-ai.com/api/v1/runs/prepare",
        "https://friday-rsi-cn.stardust-ai.com/api/internal/admin/v1/improvements/j1/publish",
    ]
    assert [r.headers["authorization"] for r in requests] == ["Bearer ordinary", "Bearer ordinary", "Bearer admin"]
    assert requests[1].headers["idempotency-key"] == "turn-key"
    assert all(r.headers["api-key"] == "test-gateway" for r in requests)


@pytest.mark.asyncio
async def test_gateway_key_can_authenticate_without_an_optional_service_bearer(monkeypatch):
    monkeypatch.delenv("COSENS_RSI_TOKEN", raising=False)
    requests = []
    def server(request):
        requests.append(request)
        return httpx.Response(200, json={"run_id": "r1", "status": "ready"})
    async with CoSensClient(api_key="test-gateway", _http_transport=httpx.MockTransport(server)) as client:
        await client.atoms.prepare(objective="task", key="key")
        client.sync_atoms.get_run("r1")
    assert len(requests) == 2
    assert all(r.headers["api-key"] == "test-gateway" and "authorization" not in r.headers for r in requests)


@pytest.mark.asyncio
async def test_missing_gateway_key_fails_before_network(monkeypatch):
    monkeypatch.delenv("COSENS_API_KEY", raising=False)
    requests = []
    async with CoSensClient(token="service-bearer", _http_transport=httpx.MockTransport(lambda r: (requests.append(r), httpx.Response(200, json={}))[1])) as client:
        with pytest.raises(ValueError, match="api_key"):
            await client.atoms.get_run("r1")
    assert requests == []


def test_gateway_turn_store_does_not_resume_unscoped_direct_service_state(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO turns VALUES (?, ?)", ("turn-old", json.dumps({"run_id": "direct-r1"})))
    store = TurnStore(path)
    try:
        assert store.get("turn-old") is None
        store.put("turn-new", {"run_id": "gateway-r1"})
        assert store.get("turn-new") == {"run_id": "gateway-r1"}
    finally:
        store.close()


def test_different_gateway_keys_cannot_read_each_others_turn_state(tmp_path):
    from openai import OpenAI
    first = cosens(OpenAI(api_key="model"), mode="disable", api_key="tenant-one", state_path=tmp_path / "state.sqlite3")
    second = cosens(OpenAI(api_key="model"), mode="disable", api_key="tenant-two", state_path=tmp_path / "state.sqlite3")
    try:
        first._runtime.turns.put("turn-1", {"run_id": "private-run"})
        assert first.cosens.get_turn("turn-1")["run_id"] == "private-run"
        assert second.cosens.get_turn("turn-1") is None
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
async def test_plain_text_gateway_rejection_retains_http_status_and_request_id():
    async with CoSensClient(api_key="invalid-test-key", _http_transport=httpx.MockTransport(
        lambda r: httpx.Response(401, text="Unauthorized", headers={"x-request-id": "gateway-request"})
    )) as client:
        with pytest.raises(RSIHTTPError) as exc:
            await client.atoms.get_run("r1")
    assert exc.value.status_code == 401
    assert exc.value.request_id == "gateway-request"
