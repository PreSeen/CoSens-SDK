import httpx
import pytest

from cosens import CoSensClient
from cosens.http import AsyncTransport, SyncTransport
from cosens.state import TurnStore


@pytest.mark.asyncio
async def test_gateway_retains_service_paths_queries_and_headers_for_both_transports():
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "ready", "run_id": "r1"})

    sync = SyncTransport("ordinary", transport=httpx.MockTransport(server))
    async_transport = AsyncTransport("ordinary", transport=httpx.MockTransport(server))
    try:
        sync.request("GET", "/v1/runs/r1?include_context=true")
        await async_transport.request("POST", "/v1/runs/prepare", body={"objective": "task"}, key="turn-key")
        async with CoSensClient(token="ordinary", admin_token="admin", _http_transport=httpx.MockTransport(server)) as client:
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
