"""Public atomic RSI resources. No atom runs an Agent or model."""

import asyncio
from urllib.parse import urlencode

from .http import AsyncTransport, SyncTransport
from ._config import resolve_api_key, resolve_service
from .errors import RSIRecoveryRequired
from .models import result
from .workflow import _bounded_timeout
from .atoms_helpers import path_part as _part, stable_key as _key
from .workflow_atoms import AsyncWorkflowAtoms, SyncWorkflowAtoms, validate_artifact
from .document_atoms import AsyncDocumentAtoms, SyncDocumentAtoms


def _recall_context(recipe, memory, query, task_family):
    if not isinstance(recipe, dict) or recipe.get("status") not in {"matched", "not_found"}:
        raise RSIRecoveryRequired("Standalone Recipe lookup returned an invalid status")
    if recipe["status"] == "matched" and (not recipe.get("recipe_id") or not isinstance(recipe.get("bundle"), dict)):
        raise RSIRecoveryRequired("Standalone Recipe bundle is missing")
    if not isinstance(memory, dict) or not isinstance(memory.get("items"), list) or not all(isinstance(item, dict) for item in memory["items"]):
        raise RSIRecoveryRequired("Standalone Memory recall items are missing or invalid")
    return result({"recipe": recipe, "context": {"query": query, "task_family": task_family, "source": "public_recipe_and_memory"},
                   "snapshot": {"evidence": memory["items"], "memory_recall": memory}})


def _bounded_integer(value, *, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _recipe_list_path(limit, cursor, task_id, status):
    params = {"limit": _bounded_integer(limit, name="limit", minimum=1, maximum=100)}
    for name, value in (("cursor", cursor), ("task_id", task_id)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ValueError(f"{name} must be a nonempty string of at most 128 characters")
            params[name] = value
    if status is not None:
        if not isinstance(status, str) or status not in {"queued", "running", "awaiting_runner", "awaiting_parent", "awaiting_material", "candidate", "failed", "interrupted"}:
            raise ValueError("invalid Recipe candidate status")
        params["status"] = status
    return "/v1/recipe-candidates?" + urlencode(params)


def _ingest_body(content, request_key, source_kind, durable, source_id, expected_revision):
    body = {"content": content, "request_key": _key(request_key), "source_kind": source_kind, "durable": durable}
    if source_id is not None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id must be a nonempty string")
        body["source_id"] = source_id
    if expected_revision is not None:
        body["expected_revision"] = _bounded_integer(expected_revision, name="expected_revision", minimum=0, maximum=2**53-1)
    return body


class AsyncAtoms(AsyncWorkflowAtoms, AsyncDocumentAtoms):
    def __init__(self, transport):
        self._http = transport

    async def list_recipe_candidates(self, *, limit=20, cursor=None, task_id=None, status=None):
        return await self._http.request("GET", _recipe_list_path(limit, cursor, task_id, status))

    async def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        page = await self.list_recipe_candidates(status='candidate', limit=100)
        recipe = {'status': 'not_found', 'candidates': page['items']}
        memory = await self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)


    async def memory_recall(self, *, query, token_budget=1000):
        return await self._http.request("POST", "/v1/memory/recall", body={"query": query, "token_budget": _bounded_integer(token_budget, name="token_budget", minimum=1, maximum=8000)})

    async def memory_ingest(self, *, content, request_key, source_kind="decision", durable=True, source_id=None, expected_revision=None):
        return await self._http.request("POST", "/v1/memory/ingest", body=_ingest_body(content, request_key, source_kind, durable, source_id, expected_revision))

    async def get_memory_items(self, *, limit=200):
        return await self._http.request("GET", "/v1/memory/items?" + urlencode({"limit": _bounded_integer(limit, name="limit", minimum=1, maximum=500)}))

    async def get_memory_job(self, job_id):
        return await self._http.request("GET", f"/v1/memory/jobs/{_part(job_id)}")

    async def get_memory_experience(self, experience_id, *, revision=None):
        suffix = "?" + urlencode({"revision": revision}) if revision is not None else ""
        return await self._http.request("GET", f"/v1/memory/experiences/{_part(experience_id)}" + suffix)

    async def get_memory(self):
        return await self._http.request("GET", "/v1/memory")


    async def artifact(self, *, name, content):
        validate_artifact(name, content)
        return await self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})


class SyncAtoms(SyncWorkflowAtoms, SyncDocumentAtoms):
    """Internal synchronous form used by wrapped synchronous OpenAI clients."""

    def __init__(self, transport):
        self._http = transport

    def list_recipe_candidates(self, *, limit=20, cursor=None, task_id=None, status=None):
        return self._http.request("GET", _recipe_list_path(limit, cursor, task_id, status))

    def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        page = self.list_recipe_candidates(status='candidate', limit=100)
        recipe = {'status': 'not_found', 'candidates': page['items']}
        memory = self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)


    def memory_recall(self, *, query, token_budget=1000):
        return self._http.request("POST", "/v1/memory/recall", body={"query": query, "token_budget": _bounded_integer(token_budget, name="token_budget", minimum=1, maximum=8000)})

    def memory_ingest(self, *, content, request_key, source_kind="decision", durable=True, source_id=None, expected_revision=None):
        return self._http.request("POST", "/v1/memory/ingest", body=_ingest_body(content, request_key, source_kind, durable, source_id, expected_revision))

    def get_memory_items(self, *, limit=200):
        return self._http.request("GET", "/v1/memory/items?" + urlencode({"limit": _bounded_integer(limit, name="limit", minimum=1, maximum=500)}))

    def get_memory_job(self, job_id):
        return self._http.request("GET", f"/v1/memory/jobs/{_part(job_id)}")

    def get_memory_experience(self, experience_id, *, revision=None):
        suffix = "?" + urlencode({"revision": revision}) if revision is not None else ""
        return self._http.request("GET", f"/v1/memory/experiences/{_part(experience_id)}" + suffix)

    def get_memory(self):
        return self._http.request("GET", "/v1/memory")


    def artifact(self, *, name, content):
        validate_artifact(name, content)
        return self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})


class CoSensClient:
    def __init__(self, api_key=None, *, region=None, state_path=None, _http_transport=None):
        self._service = resolve_service(region)
        api_key = resolve_api_key(api_key)
        self._transport = AsyncTransport(api_key=api_key, transport=_http_transport, service=self._service)
        self._sync_transport = SyncTransport(api_key=api_key, transport=_http_transport, service=self._service)
        self.atoms = AsyncAtoms(self._transport)
        self.sync_atoms = SyncAtoms(self._sync_transport)
        self.state_path = state_path

    async def aclose(self):
        await self._transport.aclose()
        self._sync_transport.close()

    async def start(self, session_context, *, phase="prepare", mode="auto", request_key=None, authorize=None, recipe_id=None, model=None, include_memory=False):
        """Start a workflow phase and return the host's next request or job receipt."""
        from .workflow_start import start_workflow
        return await start_workflow(self.atoms, session_context, phase=phase, mode=mode,
                                    request_key=request_key, authorize=authorize, recipe_id=recipe_id, model=model, include_memory=include_memory)

    def close(self):
        self._sync_transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
