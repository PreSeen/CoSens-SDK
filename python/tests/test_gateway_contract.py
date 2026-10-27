import inspect
import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIRecoveryRequired


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("matched", [False, True])
@pytest.mark.asyncio
async def test_standalone_recall_composes_public_memory_and_recipe_without_identity_header(asynchronous, matched):
    requests = []
    recipe = {"status": "matched", "recipe_id": "recipe-1", "revision": 1, "bundle": {"steps": {}}} if matched else {"status": "not_found", "task_family": "family & one"}
    memory = {"query": "task", "items": [{"id": "e1", "text": "memory evidence", "refs": []}], "estimated_tokens": 5}
    def server(request):
        requests.append(request)
        return httpx.Response(200, json=recipe if request.method == "GET" else memory)
    async with CoSensClient(api_key="gateway-test", _http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms
        value = atoms.recall_context(query="task", task_family="family & one", token_budget=1000)
        value = await value if asynchronous else value
    assert value["recipe"] == recipe
    assert value["snapshot"]["evidence"] == memory["items"]
    assert value.get("run_id") is None
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/api/v1/recipes/active"), ("POST", "/api/v1/memory/recall")]
    assert requests[0].url.params["task_family"] == "family & one"
    assert json.loads(requests[1].content) == {"query": "task", "token_budget": 1000}
    assert all(r.headers["authorization"] == "Bearer gateway-test" for r in requests)
    assert all(not any(k.lower().replace('_', '-') == 'x-user-id' for k in r.headers) for r in requests)


@pytest.mark.parametrize("recipe,memory", [
    ({"status": "matched"}, {"items": []}),
    ({"status": "not_found"}, {}),
    ({"status": "not_found"}, {"items": "invalid"}),
    ({"status": "failed"}, {"items": []}),
])
@pytest.mark.asyncio
async def test_invalid_standalone_context_is_not_silently_treated_as_empty(recipe, memory):
    async with CoSensClient(_http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json=recipe if r.method == 'GET' else memory))) as client:
        with pytest.raises(RSIRecoveryRequired):
            await client.atoms.recall_context(query="task")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_auto_skipped_uses_decided_family_and_freezes_public_recall_for_tool_loop(tmp_path, asynchronous):
    from types import SimpleNamespace
    from openai import AsyncOpenAI, OpenAI
    from cosens import cosens
    requests, business, decisions = [], [], []
    def server(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.params["task_family"] == "finance"
            return httpx.Response(200, json={"status":"matched", "recipe_id":"finance-recipe", "bundle":{"steps":{}}})
        assert json.loads(request.content)["query"] == "reconcile"
        return httpx.Response(200, json={"items":[{"text":"original frozen memory"}]})
    def create(**kwargs):
        if kwargs.get("text", {}).get("format", {}).get("name") == "cosens_decision":
            decisions.append(kwargs)
            return SimpleNamespace(output_text='{"activate":false,"objective":"reconcile","task_family":"finance","reason":"routine"}')
        business.append(kwargs)
        return SimpleNamespace(id="tool-response", status="completed", output_text="done", output=[SimpleNamespace(type="function_call" if len(business)==1 else "message")])
    async def acreate(**kwargs):
        return create(**kwargs)
    native = (AsyncOpenAI if asynchronous else OpenAI)(api_key="offline-model")
    native.responses.create = acreate if asynchronous else create
    client = cosens(native, api_key="gateway", mode="auto", state_path=tmp_path / "state.sqlite3", _http_transport=httpx.MockTransport(server))
    try:
        for kwargs in [{"input":"task"}, {"input":[{"type":"function_call_output", "call_id":"c1", "output":"result"}], "previous_response_id":"tool-response"}]:
            value = client.responses.create(model="app", **kwargs)
            if asynchronous: await value
        assert len(requests) == 2 and len(decisions) == 1 and len(business) == 2
        assert all("finance-recipe" in b['instructions'] and "original frozen memory" in b['instructions'] for b in business)
        assert client.cosens.get_turn(client.cosens.last_turn_id)['run_id'] is None
    finally:
        if asynchronous: await client.close()
        else: client.close()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_transcript_atomic_requests_keep_envelopes_and_encoded_identifiers(asynchronous):
    observed = []
    def server(request):
        observed.append((request.method, request.url.raw_path.decode(), json.loads(request.content) if request.content else None))
        assert not any(k.lower().replace('_', '-') == 'x-user-id' for k in request.headers)
        return httpx.Response(200, json={"status": "saved"})
    async with CoSensClient(_http_transport=httpx.MockTransport(server)) as client:
        atoms = client.atoms if asynchronous else client.sync_atoms
        calls = [
            (atoms.memory_ingest, {"content":"test", "request_key":"stable", "source_kind":"decision", "durable":True}),
            (atoms.get_memory_job, {"job_id":"j/1"}),
            (atoms.get_memory_experience, {"experience_id":"e/1", "revision":1}),
            (atoms.get_memory, {}),
            (atoms.get_recipe_step, {"run_id":"r1", "node_id":"produce"}),
            (atoms.guidance, {"run_id":"r1", "data":{"observation":"task", "candidates":[], "request_key":"choose"}}),
            (atoms.checkpoint, {"run_id":"r1", "data":{"decision_id":"d1", "actual_action_id":"a1"}}),
            (atoms.get_decision, {"run_id":"r1", "decision_id":"d1"}),
            (atoms.get_trace, {"run_id":"r1"}),
        ]
        for method, kwargs in calls:
            value = method(**kwargs)
            if inspect.isawaitable(value): await value
        await client.admin.atoms.bootstrap_bundle(task_family="isolated", bundle={"steps":{}}, reason="test")
        await client.admin.atoms.evaluate_decision("r1", "d1", evaluation={"evaluation_id":"eval-1", "value":-1})
        await client.admin.atoms.evaluate_recipe("r1", record={"evaluation_id":"recipe-eval-1"}, artifact_id="a1", content_hash="h1")
    assert observed == [
        ("POST", "/api/v1/memory/ingest", {"content":"test", "request_key":"stable", "source_kind":"decision", "durable":True}),
        ("GET", "/api/v1/memory/jobs/j%2F1", None),
        ("GET", "/api/v1/memory/experiences/e%2F1?revision=1", None),
        ("GET", "/api/v1/memory", None),
        ("GET", "/api/v1/runs/r1/recipe/steps/produce", None),
        ("POST", "/api/v1/runs/r1/guidance", {"data":{"observation":"task", "candidates":[], "request_key":"choose"}}),
        ("POST", "/api/v1/runs/r1/checkpoint", {"data":{"decision_id":"d1", "actual_action_id":"a1"}}),
        ("GET", "/api/v1/runs/r1/decisions/d1", None),
        ("GET", "/api/v1/runs/r1/trace", None),
        ("POST", "/api/internal/admin/v1/recipes/bootstrap-bundle", {"task_family":"isolated", "bundle":{"steps":{}}, "reason":"test"}),
        ("POST", "/api/internal/admin/v1/runs/r1/decisions/d1/evaluations", {"evaluation_id":"eval-1", "value":-1}),
        ("POST", "/api/internal/admin/v1/runs/r1/recipe-evaluation", {"record":{"evaluation_id":"recipe-eval-1"}, "artifact_id":"a1", "content_hash":"h1"}),
    ]
