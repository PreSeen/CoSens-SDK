"""Public atomic RSI resources. No atom runs an Agent or model."""

import asyncio
import os
from urllib.parse import quote, urlencode

from .http import AsyncTransport, SyncTransport
from .errors import RSIRecoveryRequired
from .models import result


def _part(value):
    if not isinstance(value, str) or not value:
        raise ValueError("resource ID must be a nonempty string")
    return quote(value, safe="")


def _key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("key must be 1–256 characters")
    return value


def _recall_context(recipe, memory, query, task_family):
    if not isinstance(recipe, dict) or recipe.get("status") not in {"matched", "not_found"}:
        raise RSIRecoveryRequired("Standalone Recipe lookup returned an invalid status")
    if recipe["status"] == "matched" and (not recipe.get("recipe_id") or not isinstance(recipe.get("bundle"), dict)):
        raise RSIRecoveryRequired("Standalone Recipe bundle is missing")
    if not isinstance(memory, dict) or not isinstance(memory.get("items"), list) or not all(isinstance(item, dict) for item in memory["items"]):
        raise RSIRecoveryRequired("Standalone Memory recall items are missing or invalid")
    return result({"recipe": recipe, "context": {"query": query, "task_family": task_family, "source": "public_recipe_and_memory"},
                   "snapshot": {"evidence": memory["items"], "memory_recall": memory}})


class AsyncAtoms:
    def __init__(self, transport):
        self._http = transport

    async def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        recipe = await self.get_active_recipe(task_family=task_family)
        memory = await self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)

    async def get_active_recipe(self, *, task_family="general"):
        return await self._http.request("GET", "/v1/recipes/active?" + urlencode({"task_family": task_family}))

    async def memory_recall(self, *, query, token_budget=1000):
        return await self._http.request("POST", "/v1/memory/recall", body={"query": query, "token_budget": token_budget})

    async def memory_ingest(self, *, content, request_key, source_kind="decision", durable=True):
        return await self._http.request("POST", "/v1/memory/ingest", body={"content": content, "request_key": _key(request_key), "source_kind": source_kind, "durable": durable})

    async def get_memory_job(self, job_id):
        return await self._http.request("GET", f"/v1/memory/jobs/{_part(job_id)}")

    async def get_memory_experience(self, experience_id, *, revision=None):
        suffix = "?" + urlencode({"revision": revision}) if revision is not None else ""
        return await self._http.request("GET", f"/v1/memory/experiences/{_part(experience_id)}" + suffix)

    async def get_memory(self):
        return await self._http.request("GET", "/v1/memory")

    async def get_recipe_step(self, run_id, node_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}/recipe/steps/{_part(node_id)}")

    async def guidance(self, run_id, *, data):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/guidance", body={"data": data})

    async def checkpoint(self, run_id, *, data):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/checkpoint", body={"data": data})

    async def get_decision(self, run_id, decision_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}/decisions/{_part(decision_id)}")

    async def get_trace(self, run_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}/trace")

    async def artifact(self, *, name, content):
        return await self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    async def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = {"objective": objective, "task_family": task_family, "input_artifact_ids": input_artifact_ids or []}
        body.update(options)
        return await self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    async def get_run(self, run_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    async def session_events(self, run_id, events):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    async def initial_inputs(self, run_id, artifact_ids):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    async def submit(self, run_id, *, data=None, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key else None)

    async def feedback(self, run_id, *, data, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/feedback", body={"data": data}, key=_key(key) if key else None)

    async def improve(self, run_id, *, reason, key=None):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/improvements", body={"data": {"reason": reason}}, key=_key(key) if key else None)

    async def get_improvement(self, job_id):
        return await self._http.request("GET", f"/v1/improvements/{_part(job_id)}")

    async def resume_improvement(self, job_id):
        return await self._http.request("POST", f"/v1/improvements/{_part(job_id)}/resume")

    async def wait_improvement(self, job_id, *, timeout=300, initial_delay=2, max_delay=30):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        delay = initial_delay
        while True:
            job = await self.get_improvement(job_id)
            if job.status not in {"queued", "running"}:
                return job
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"improvement {job_id} did not finish within {timeout} seconds")
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, max_delay)


class AdminAtoms:
    def __init__(self, transport):
        self._http = transport

    async def bootstrap_bundle(self, *, task_family, bundle, reason):
        return await self._http.request("POST", "/internal/admin/v1/recipes/bootstrap-bundle", body={"task_family": task_family, "bundle": bundle, "reason": reason})

    async def evaluate_decision(self, run_id, decision_id, *, evaluation):
        return await self._http.request("POST", f"/internal/admin/v1/runs/{_part(run_id)}/decisions/{_part(decision_id)}/evaluations", body=evaluation)

    async def evaluate_recipe(self, run_id, *, record, artifact_id, content_hash):
        return await self._http.request("POST", f"/internal/admin/v1/runs/{_part(run_id)}/recipe-evaluation", body={"record": record, "artifact_id": artifact_id, "content_hash": content_hash})

    async def publish(self, job_id, *, expected_revision, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        return await self._http.request(
            "POST", f"/internal/admin/v1/improvements/{_part(job_id)}/publish",
            body={"expected_revision": expected_revision, "reason": reason},
        )


class _AdminClient:
    def __init__(self, transport):
        self.atoms = AdminAtoms(transport)


class SyncAtoms:
    """Internal synchronous form used by wrapped synchronous OpenAI clients."""

    def __init__(self, transport):
        self._http = transport

    def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        recipe = self.get_active_recipe(task_family=task_family)
        memory = self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)

    def get_active_recipe(self, *, task_family="general"):
        return self._http.request("GET", "/v1/recipes/active?" + urlencode({"task_family": task_family}))

    def memory_recall(self, *, query, token_budget=1000):
        return self._http.request("POST", "/v1/memory/recall", body={"query": query, "token_budget": token_budget})

    def memory_ingest(self, *, content, request_key, source_kind="decision", durable=True):
        return self._http.request("POST", "/v1/memory/ingest", body={"content": content, "request_key": _key(request_key), "source_kind": source_kind, "durable": durable})

    def get_memory_job(self, job_id):
        return self._http.request("GET", f"/v1/memory/jobs/{_part(job_id)}")

    def get_memory_experience(self, experience_id, *, revision=None):
        suffix = "?" + urlencode({"revision": revision}) if revision is not None else ""
        return self._http.request("GET", f"/v1/memory/experiences/{_part(experience_id)}" + suffix)

    def get_memory(self):
        return self._http.request("GET", "/v1/memory")

    def get_recipe_step(self, run_id, node_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}/recipe/steps/{_part(node_id)}")

    def guidance(self, run_id, *, data):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/guidance", body={"data": data})

    def checkpoint(self, run_id, *, data):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/checkpoint", body={"data": data})

    def get_decision(self, run_id, decision_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}/decisions/{_part(decision_id)}")

    def get_trace(self, run_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}/trace")

    def artifact(self, *, name, content):
        return self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = {"objective": objective, "task_family": task_family, "input_artifact_ids": input_artifact_ids or []}
        body.update(options)
        return self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    def get_run(self, run_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    def session_events(self, run_id, events):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    def initial_inputs(self, run_id, artifact_ids):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    def submit(self, run_id, *, data=None, key=None):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key else None)


class CoSensClient:
    def __init__(self, api_key=None, *, state_path=None, _http_transport=None):
        api_key = api_key if api_key is not None else (os.getenv("RSI_API_KEY") or os.getenv("COSENS_API_KEY"))
        self._transport = AsyncTransport(api_key=api_key, transport=_http_transport)
        self._admin_transport = AsyncTransport(api_key=api_key, transport=_http_transport)
        self._sync_transport = SyncTransport(api_key=api_key, transport=_http_transport)
        self.atoms = AsyncAtoms(self._transport)
        self.sync_atoms = SyncAtoms(self._sync_transport)
        self.admin = _AdminClient(self._admin_transport)
        self.state_path = state_path

    async def aclose(self):
        await self._transport.aclose()
        await self._admin_transport.aclose()
        self._sync_transport.close()

    def close(self):
        self._sync_transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
