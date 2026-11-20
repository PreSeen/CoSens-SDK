import asyncio
import json
from functools import partial

import httpx
import pytest

from cosens import (
    MemoryError,
    get_memory,
    get_memory_experience,
    get_memory_items,
    get_memory_job,
    memory_ingest,
    memory_recall,
)


def test_memory_atoms_1026_contract(monkeypatch):
    requests = []
    prefix = "/api/v1/memory"

    def mock(request):
        assert request.url.host == "rsi.cosens.ai"
        assert request.headers["authorization"] == "Bearer fixture-key"
        assert request.headers["x-auth-type"] == "api_key"
        requests.append(
            (
                request.method,
                request.url.raw_path.decode(),
                json.loads(request.content) if request.content else None,
            )
        )
        return httpx.Response(200, json={"items": [{"id": "example"}]})

    monkeypatch.setenv("COSENS_API_KEY", "fixture-key")
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(mock))
    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def run():
        assert (await memory_recall(query="intent"))["items"] == [{"id": "example"}]
        await memory_ingest(
            content="decision",
            request_key="request-1",
            durable=False,
            source_id="source-1",
            expected_revision=0,
        )
        await get_memory_items()
        await get_memory_job("job/1")
        await get_memory_experience("exp/1", revision=2)
        await get_memory()
        with pytest.raises(ValueError):
            await get_memory_job("..")
        with pytest.raises(ValueError):
            await memory_recall(query="intent", token_budget=8001)

    asyncio.run(run())
    assert requests == [
        ("POST", prefix + "/recall", {"query": "intent", "token_budget": 1000}),
        (
            "POST",
            prefix + "/ingest",
            {
                "content": "decision",
                "request_key": "request-1",
                "source_kind": "decision",
                "durable": False,
                "source_id": "source-1",
                "expected_revision": 0,
            },
        ),
        ("GET", prefix + "/items?limit=200", None),
        ("GET", prefix + "/jobs/job%2F1", None),
        ("GET", prefix + "/experiences/exp%2F1?revision=2", None),
        ("GET", prefix, None),
    ]


def test_memory_errors_redact_key_and_reject_redirects(monkeypatch):
    def mock(request):
        assert request.url.host == "rsi.cosens.ai"
        if request.url.path.endswith("/recall"):
            return httpx.Response(302, headers={"location": "https://example.test/"})
        return httpx.Response(
            401,
            json={"error": {"message": "invalid fixture-key", "code": "denied"}},
            headers={"x-request-id": "request-1"},
        )

    monkeypatch.setenv("COSENS_API_KEY", "fixture-key")
    client = partial(httpx.AsyncClient, transport=httpx.MockTransport(mock))
    monkeypatch.setattr(httpx, "AsyncClient", client)

    async def run():
        with pytest.raises(MemoryError, match=r"invalid \[REDACTED\]") as exc:
            await get_memory()
        assert (exc.value.status, exc.value.code, exc.value.request_id) == (
            401,
            "denied",
            "request-1",
        )
        with pytest.raises(MemoryError, match="redirects are not followed") as exc:
            await memory_recall(query="intent")
        assert exc.value.status == 302

    asyncio.run(run())


def test_virtual_user_selection_is_per_call_and_unsupported_methods_reject(monkeypatch):
    seen = []
    def mock(request):
        seen.append((request.url.path, request.headers.get("x-virtual-user-id")))
        return httpx.Response(200, json={})
    monkeypatch.setenv("COSENS_API_KEY", "fixture-key")
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(mock)))
    async def run():
        await asyncio.gather(
            memory_recall(query="a", virtual_user_id="vu_a"),
            memory_ingest(content="b", request_key="k", virtual_user_id="vu_b"),
            get_memory_items(virtual_user_id="vu_b"), get_memory_job("j", virtual_user_id="vu_a"), get_memory_items(),
        )
        with pytest.raises(ValueError):
            await get_memory(virtual_user_id="vu_a")
        with pytest.raises(ValueError):
            await get_memory_experience("e", virtual_user_id="vu_a")
    asyncio.run(run())
    assert set(seen) == {("/api/v1/memory/recall", "vu_a"), ("/api/v1/memory/ingest", "vu_b"), ("/api/v1/memory/items", None), ("/api/v1/memory/items", "vu_b"), ("/api/v1/memory/jobs/j", "vu_a")}
