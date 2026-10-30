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
from .model_api import improvement_data


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
        if not isinstance(status, str) or status not in {"queued", "running", "awaiting_runner", "candidate", "failed"}:
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


def _step_body(attempt_token, outputs, decision_id, actual_action_id):
    if not isinstance(attempt_token, str) or not attempt_token:
        raise ValueError("attempt_token is required")
    if not isinstance(outputs, dict):
        raise TypeError("outputs must be an object")
    body = {"attempt_token": attempt_token, "outputs": outputs}
    if decision_id is not None:
        body["decision_id"] = decision_id
    if actual_action_id is not None:
        body["actual_action_id"] = actual_action_id
    return body


def _prepare_body(objective, task_family, input_artifact_ids, options):
    execution_mode = options.get("execution_mode", "assist")
    if execution_mode not in {"assist", "guided"}:
        raise ValueError("execution_mode must be assist or guided")
    return {"objective": objective, "task_family": task_family, "input_artifact_ids": input_artifact_ids or [], **options}


class AsyncAtoms(AsyncWorkflowAtoms, AsyncDocumentAtoms):
    def __init__(self, transport):
        self._http = transport

    async def list_recipe_candidates(self, *, limit=20, cursor=None, task_id=None, status=None):
        return await self._http.request("GET", _recipe_list_path(limit, cursor, task_id, status))

    async def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        recipe = await self.get_active_recipe(task_family=task_family)
        memory = await self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)

    async def get_active_recipe(self, *, task_family="general"):
        return await self._http.request("GET", "/v1/recipes/active?" + urlencode({"task_family": task_family}))

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

    async def next_step(self, run_id):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/steps/next")

    async def submit_step(self, run_id, attempt_id, *, attempt_token, outputs, decision_id=None, actual_action_id=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/steps/{_part(attempt_id)}/submit",
                                        body=_step_body(attempt_token, outputs, decision_id, actual_action_id))

    async def complete_guided(self, run_id):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/complete")

    async def get_events(self, run_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}/events")

    async def artifact(self, *, name, content):
        validate_artifact(name, content)
        return await self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    async def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = _prepare_body(objective, task_family, input_artifact_ids, options)
        return await self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    async def get_run(self, run_id):
        return await self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    async def session_events(self, run_id, events):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    async def initial_inputs(self, run_id, artifact_ids):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    async def submit(self, run_id, *, data=None, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key is not None else None)

    async def feedback(self, run_id, *, data, key=None):
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/feedback", body={"data": data}, key=_key(key) if key is not None else None)

    async def improve(self, run_id, *, reason, key=None, feedback=None, model_api=None):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required")
        data, secrets = improvement_data(reason=reason, feedback=feedback, model_api=model_api)
        options = {"sensitive_values": secrets} if secrets else {}
        return await self._http.request("POST", f"/v1/runs/{_part(run_id)}/improvements", body={"data": data}, key=_key(key) if key is not None else None, **options)

    async def get_improvement(self, job_id):
        return await self._http.request("GET", f"/v1/improvements/{_part(job_id)}")

    async def resume_improvement(self, job_id, *, model_api=None):
        if model_api is None:
            return await self._http.request("POST", f"/v1/improvements/{_part(job_id)}/resume")
        data, secrets = improvement_data(model_api=model_api)
        return await self._http.request("POST", f"/v1/improvements/{_part(job_id)}/resume", body={"data": data}, sensitive_values=secrets)

    async def confirm_attribution(self, job_id, *, candidate_id, candidate_sha256):
        return await self._http.request("POST", f"/v1/improvements/{_part(job_id)}/attribution/confirm",
                                        body={"data": {"candidate_id": candidate_id, "candidate_sha256": candidate_sha256}})

    async def wait_improvement(self, job_id, *, timeout=300, initial_delay=2, max_delay=30):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _bounded_timeout(timeout)
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


class SyncAtoms(SyncWorkflowAtoms, SyncDocumentAtoms):
    """Internal synchronous form used by wrapped synchronous OpenAI clients."""

    def __init__(self, transport):
        self._http = transport

    def list_recipe_candidates(self, *, limit=20, cursor=None, task_id=None, status=None):
        return self._http.request("GET", _recipe_list_path(limit, cursor, task_id, status))

    def recall_context(self, *, query, task_family="general", token_budget=1000):
        """Read Recipe and Memory without creating a Run; freeze per turn upstream."""
        recipe = self.get_active_recipe(task_family=task_family)
        memory = self.memory_recall(query=query, token_budget=token_budget)
        return _recall_context(recipe, memory, query, task_family)

    def get_active_recipe(self, *, task_family="general"):
        return self._http.request("GET", "/v1/recipes/active?" + urlencode({"task_family": task_family}))

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

    def next_step(self, run_id):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/steps/next")

    def submit_step(self, run_id, attempt_id, *, attempt_token, outputs, decision_id=None, actual_action_id=None):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/steps/{_part(attempt_id)}/submit",
                                  body=_step_body(attempt_token, outputs, decision_id, actual_action_id))

    def complete_guided(self, run_id):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/complete")

    def get_events(self, run_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}/events")

    def artifact(self, *, name, content):
        validate_artifact(name, content)
        return self._http.request("POST", "/v1/artifacts", body={"name": name, "content": content})

    def prepare(self, *, objective, key, task_family="general", input_artifact_ids=None, **options):
        body = _prepare_body(objective, task_family, input_artifact_ids, options)
        return self._http.request("POST", "/v1/runs/prepare", body=body, key=_key(key))

    def get_run(self, run_id):
        return self._http.request("GET", f"/v1/runs/{_part(run_id)}")

    def session_events(self, run_id, events):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/session-events", body={"events": events})

    def initial_inputs(self, run_id, artifact_ids):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/initial-inputs", body={"artifact_ids": artifact_ids})

    def submit(self, run_id, *, data=None, key=None):
        return self._http.request("POST", f"/v1/runs/{_part(run_id)}/submit", body={"data": data or {}}, key=_key(key) if key is not None else None)


class CoSensClient:
    def __init__(self, api_key=None, *, state_path=None, _http_transport=None):
        self._service = resolve_service()
        api_key = resolve_api_key(api_key, self._service)
        self._transport = AsyncTransport(api_key=api_key, transport=_http_transport, service=self._service)
        self._admin_transport = AsyncTransport(api_key=api_key, transport=_http_transport, service=self._service)
        self._sync_transport = SyncTransport(api_key=api_key, transport=_http_transport, service=self._service)
        self.atoms = AsyncAtoms(self._transport)
        self.sync_atoms = SyncAtoms(self._sync_transport)
        self.admin = _AdminClient(self._admin_transport)
        self.state_path = state_path

    async def aclose(self):
        await self._transport.aclose()
        await self._admin_transport.aclose()
        self._sync_transport.close()

    async def start(self, session_context, *, phase="prepare", mode="auto", request_key=None, authorize=None):
        """Start a workflow phase and return the host's next request or job receipt."""
        from .workflow_start import start_workflow
        return await start_workflow(self.atoms, session_context, phase=phase, mode=mode,
                                    request_key=request_key, authorize=authorize)

    def close(self):
        self._sync_transport.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
