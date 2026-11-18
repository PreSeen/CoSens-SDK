import json

import httpx
import pytest

from cosens import CoSensClient, RSIHTTPError

RECIPE = {"title": "T", "when": "W", "when_not": "N", "steps": [{"id": "s1", "instruction": "I"}], "lessons": []}


async def test_recipe_store_requests_sync_and_async():
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    async with CoSensClient(api_key="test-key", _http_transport=httpx.MockTransport(server)) as client:
        await client.atoms.upload_recipe("team_recipe-1", version=4, recipe=RECIPE, previous_version=3)
        client.sync_atoms.upload_recipe("team_recipe-1", version=1, recipe=RECIPE)
        await client.atoms.get_recipe("g1", version=2)
        client.sync_atoms.get_recipe("r1")
        await client.atoms.list_recipes(limit=5, cursor="c1", origin="uploaded")
        client.sync_atoms.list_recipes()
        await client.atoms.search_recipes("sort words", limit=3)
        client.sync_atoms.search_recipes("sort words")

    assert [(r.method, r.url.path, dict(r.url.params)) for r in requests] == [
        ("PUT", "/api/recipes/team_recipe-1/versions/4", {}),
        ("PUT", "/api/recipes/team_recipe-1/versions/1", {}),
        ("GET", "/api/recipes/g1", {"version": "2"}),
        ("GET", "/api/recipes/r1", {}),
        ("GET", "/api/recipes", {"limit": "5", "cursor": "c1", "origin": "uploaded"}),
        ("GET", "/api/recipes", {"limit": "20"}),
        ("GET", "/api/recipes", {"q": "sort words", "limit": "3"}),
        ("GET", "/api/recipes", {"q": "sort words", "limit": "10"}),
    ]
    assert json.loads(requests[0].content) == {"previous_version": 3, "recipe": RECIPE}
    assert json.loads(requests[1].content) == {"recipe": RECIPE}


@pytest.mark.parametrize("call", [
    lambda a: a.upload_recipe("a/b", version=1, recipe=RECIPE),
    lambda a: a.upload_recipe("x" * 129, version=1, recipe=RECIPE),
    lambda a: a.upload_recipe("r", version=0, recipe=RECIPE),
    lambda a: a.upload_recipe("r", version=True, recipe=RECIPE),
    lambda a: a.upload_recipe("r", version=2, recipe=RECIPE, previous_version=0),
    lambda a: a.upload_recipe("r", version=1, recipe=[]),
    lambda a: a.get_recipe("r", version=0),
    lambda a: a.set_current_recipe_version("", version=2, expected_current_version=1),
    lambda a: a.set_current_recipe_version("r", version=0, expected_current_version=1),
    lambda a: a.set_current_recipe_version("r", version=2, expected_current_version=True),
    lambda a: a.list_recipes(limit=0),
    lambda a: a.list_recipes(limit=101),
    lambda a: a.list_recipes(cursor=" "),
    lambda a: a.list_recipes(origin="copied"),
    lambda a: a.search_recipes(""),
    lambda a: a.search_recipes("x" * 1001),
    lambda a: a.search_recipes("x", limit=51),
])
def test_recipe_store_rejects_invalid_parameters(call):
    client = CoSensClient(api_key="test-key", _http_transport=httpx.MockTransport(lambda request: pytest.fail("unexpected request")))
    try:
        with pytest.raises(ValueError):
            call(client.sync_atoms)
    finally:
        client.close()


async def test_set_current_recipe_version_sends_the_expected_version_sync_and_async():
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json={"recipe_id": "r1", "current_version": 2, "previous_version": 3,
                                         "interrupted_learnings": [{"execution_id": "e4", "version": 4, "status": "failed"}]})

    async with CoSensClient(api_key="test-key", _http_transport=httpx.MockTransport(server)) as client:
        moved = await client.atoms.set_current_recipe_version("r1", version=2, expected_current_version=3)
        client.sync_atoms.set_current_recipe_version("r1", version=2, expected_current_version=3)
    assert (moved.current_version, moved.previous_version) == (2, 3)
    assert moved.interrupted_learnings == [{"execution_id": "e4", "version": 4, "status": "failed"}]
    assert [(r.method, r.url.path, json.loads(r.content), r.headers.get("idempotency-key")) for r in requests] == [
        ("PUT", "/api/recipes/r1/current", {"version": 2, "expected_current_version": 3}, None)] * 2


async def test_a_stale_expected_current_version_is_a_conflict_with_the_current_version():
    payload = {"error": {"code": "recipe_version_conflict", "message": "the current version changed", "current_version": 4}}
    async with CoSensClient(api_key="test-key",
                            _http_transport=httpx.MockTransport(lambda request: httpx.Response(409, json=payload))) as client:
        with pytest.raises(RSIHTTPError) as caught:
            await client.atoms.set_current_recipe_version("r1", version=2, expected_current_version=3)
    assert (caught.value.status_code, caught.value.code, caught.value.current_version) == (409, "recipe_version_conflict", 4)
