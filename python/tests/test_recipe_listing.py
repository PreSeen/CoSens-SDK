import httpx
import pytest

from cosens import CoSensClient


@pytest.mark.asyncio
async def test_recipe_listing_sync_and_async_requests():
    requests = []
    pages = [
        {"user_id": "alice", "items": [{"recipe_candidate_id": "r1"}], "has_more": True, "next_cursor": "r1"},
        {"user_id": "alice", "items": [], "has_more": False, "next_cursor": None},
    ]

    def server(request):
        requests.append(request)
        return httpx.Response(200, json=pages[len(requests) - 1])

    async with CoSensClient(api_key="test-key", _http_transport=httpx.MockTransport(server)) as client:
        first = await client.atoms.list_recipe_candidates(limit=1, task_id="task /1", status="candidate")
        second = client.sync_atoms.list_recipe_candidates(limit=1, cursor=first.next_cursor, task_id="task /1", status="candidate")

    assert first.has_more and not second.has_more
    assert [request.url.path for request in requests] == ["/api/v1/recipe-candidates"] * 2
    assert dict(requests[0].url.params) == {"limit": "1", "task_id": "task /1", "status": "candidate"}
    assert dict(requests[1].url.params) == {"limit": "1", "cursor": "r1", "task_id": "task /1", "status": "candidate"}
    assert all(request.headers["authorization"] == "Bearer test-key" and request.headers["x-auth-type"] == "api_key" for request in requests)
    assert all("x-user-id" not in request.headers for request in requests)


@pytest.mark.parametrize("kwargs", [
    {"limit": 0}, {"limit": 101}, {"limit": True}, {"cursor": " "},
    {"cursor": "x" * 129}, {"task_id": ""}, {"status": "unknown"},
])
def test_recipe_listing_rejects_invalid_parameters(kwargs):
    client = CoSensClient(api_key="test-key", _http_transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request")))
    try:
        with pytest.raises(ValueError):
            client.sync_atoms.list_recipe_candidates(**kwargs)
    finally:
        client.close()
