"""HTTP atoms for the task / revision / Recipe candidate workflow.

These methods send exactly one request. Agent execution, consent and retries belong
to the caller; in particular a lost artifact receipt must not trigger re-upload.
"""

import asyncio
import json
import math
import time

from .atoms_helpers import path_part, stable_key
from .errors import RSIRecoveryRequired


def _model_data(model):
    if model is None:
        return {}
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError('model must be a public Friday model ID')
    return {'model': model.strip()}


def _completion_tokens(value):
    if value is not None and (type(value) is not int or not 1 <= value <= 1_000_000):
        raise ValueError('max_completion_tokens must be an integer from 1 to 1000000')
    return value


def _recipe_data(model, max_completion_tokens):
    data = _model_data(model)
    if _completion_tokens(max_completion_tokens) is not None:
        data['max_completion_tokens'] = max_completion_tokens
    return data


_ROLES = {"original_input", "submitted_output", "acceptance", "external_trace"}
_JOB_STATES = {
    "recipe_candidate": {"queued", "running", "awaiting_runner", "awaiting_parent", "awaiting_material", "candidate", "failed", "interrupted"},
}


def _job_timeout(timeout):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite nonnegative number")
    return timeout


def _job_getter(owner, kind):
    names = {"recipe_candidate": "get_recipe_candidate"}
    if kind not in names:
        raise ValueError("job kind must be recipe_candidate")
    return getattr(owner, names[kind])


def validate_artifact(name, content):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("artifact name is required")
    if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 1_000_000:
        raise ValueError("artifact content must be nonempty UTF-8 text of at most 1,000,000 bytes")


def _task(source, objective, artifacts, self_contained):
    if source not in {"new_task", "external_artifact"}:
        raise ValueError("source must be new_task or external_artifact")
    if objective is not None and (not isinstance(objective, str) or not objective.strip() or len(objective) > 20_000):
        raise ValueError("objective must be nonempty text of at most 20,000 characters")
    artifacts = artifacts if artifacts is not None else []
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("artifacts must contain at most 100 references")
    roles = []
    for artifact in artifacts:
        if isinstance(artifact, str) and artifact:
            roles.append("original_input" if source == "new_task" else "submitted_output")
        elif isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str) and artifact["artifact_id"] and artifact.get("role") in _ROLES:
            roles.append(artifact["role"])
        else:
            raise ValueError("invalid artifact reference or role")
    if source == "external_artifact" and "submitted_output" not in roles:
        raise ValueError("external_artifact requires submitted_output")
    role_by_id = {}
    for artifact, role in zip(artifacts, roles):
        artifact_id = artifact if isinstance(artifact, str) else artifact["artifact_id"]
        role_by_id.setdefault(artifact_id, set()).add(role)
    if any({"original_input", "submitted_output"}.issubset(item_roles) for item_roles in role_by_id.values()):
        raise ValueError("one artifact cannot be both original_input and submitted_output")
    if self_contained is not None and not isinstance(self_contained, bool):
        raise ValueError("self_contained must be a boolean")
    body = {"source": source, "artifacts": artifacts}
    if objective is not None:
        body["objective"] = objective
    if self_contained is not None:
        body["self_contained"] = self_contained
    return body


def _context(expected_context_version, objective, artifacts, self_contained):
    if not isinstance(expected_context_version, int) or isinstance(expected_context_version, bool) or expected_context_version < 1:
        raise ValueError("expected_context_version must be a positive integer")
    body = {"expected_context_version": expected_context_version}
    for field, value in (("objective", objective), ("artifacts", artifacts), ("self_contained", self_contained)):
        if value is not None:
            body[field] = value
    return body


def _result(request_sha256, outcome, artifacts, events, trace_coverage):
    if not isinstance(request_sha256, str) or not request_sha256.strip():
        raise ValueError("request_sha256 is required")
    if outcome not in {"completed", "failed", "cancelled"}:
        raise ValueError("outcome must be completed, failed or cancelled")
    if trace_coverage not in {"full", "partial", "unknown"}:
        raise ValueError("trace_coverage must be full, partial or unknown")
    if not isinstance(artifacts, list) or not isinstance(events, list) or len(events) > 200:
        raise ValueError("artifacts must be a list and events must contain at most 200 entries")
    ids = [event.get("id") for event in events if isinstance(event, dict)]
    if len(ids) != len(events) or any(not isinstance(event_id, str) or not event_id for event_id in ids) or len(set(ids)) != len(ids):
        raise ValueError("event IDs must be nonempty and unique")
    if any(not isinstance(event.get("kind"), str) or not event["kind"] or not isinstance(event.get("payload"), dict) for event in events):
        raise ValueError("event kind must be nonempty and payload must be an object")
    if any(not isinstance(a, dict) or not isinstance(a.get("artifact_id"), str) or not a["artifact_id"] or a.get("role") != "submitted_output" for a in artifacts):
        raise ValueError("execution artifacts must be submitted_output references")
    if outcome == "completed" and not any(isinstance(a, dict) and a.get("role") == "submitted_output" for a in artifacts):
        raise ValueError("completed execution requires a submitted_output artifact")
    body = {"request_sha256": request_sha256, "outcome": outcome, "artifacts": artifacts,
            "events": events, "trace_coverage": trace_coverage}
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 1_000_000:
        raise ValueError("execution result JSON must be at most 1,000,000 bytes")
    return body


class AsyncWorkflowAtoms:
    async def wait_workflow_job(self, kind, job_id, *, timeout=300):
        """Bounded polling of an existing job ID; never creates or resumes a job."""
        getter = _job_getter(self, kind)
        deadline = asyncio.get_running_loop().time() + _job_timeout(timeout)
        delay = 2.0
        while True:
            job = await getter(job_id)
            status = job.get("status")
            if status not in _JOB_STATES[kind]:
                raise RSIRecoveryRequired(f"Unexpected workflow {kind} status: {status}")
            if status not in {"queued", "running", "awaiting_parent"}:
                return job
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Workflow {kind} {job_id} is still {status}; resume polling the same ID")
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    async def get_artifact(self, artifact_id):
        return await self._http.request("GET", f"/v1/artifacts/{path_part(artifact_id)}")

    async def create_task(self, *, source, key, objective=None, artifacts=None, self_contained=None):
        return await self._http.request("POST", "/v1/tasks", body=_task(source, objective, artifacts, self_contained), key=stable_key(key))

    async def get_task(self, task_id):
        return await self._http.request("GET", f"/v1/tasks/{path_part(task_id)}")

    async def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None, self_contained=None):
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/context",
                                        body=_context(expected_context_version, objective, artifacts, self_contained))


    async def start_execution(self, task_id, *, key, recipe_id=None, model=None, include_memory=False):
        data = _model_data(model)
        if type(include_memory) is not bool:
            raise ValueError('include_memory must be boolean')
        if include_memory:
            data['include_memory'] = True
        if recipe_id is not None:
            path_part(recipe_id)
            data['recipe_id'] = recipe_id
        options = {'body': data} if data else {}
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/executions", key=stable_key(key), **options)

    async def get_task_recipe(self, task_id):
        return await self._http.request('GET', f'/v1/tasks/{path_part(task_id)}/recipe')

    async def get_recipe_versions(self, recipe_id):
        return await self._http.request('GET', f'/v1/recipe-chains/{path_part(recipe_id)}/versions')


    async def get_execution(self, execution_id):
        return await self._http.request("GET", f"/v1/executions/{path_part(execution_id)}")

    async def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return await self._http.request("POST", f"/v1/executions/{path_part(execution_id)}/result",
                                        body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    async def get_revision(self, revision_id):
        return await self._http.request("GET", f"/v1/revisions/{path_part(revision_id)}")


    async def start_revision(self, revision_id, *, key):
        return await self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/revisions",
                                        body={"data": {}}, key=stable_key(key))


    async def start_recipe_candidate(self, task_id, *, revision_id, key, model=None, max_completion_tokens=None):
        data = _recipe_data(model, max_completion_tokens)
        data.update(revision_id=revision_id)
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/recipe-candidates",
                                        body={"data": data}, key=stable_key(key))

    async def get_recipe_candidate(self, recipe_id):
        return await self._http.request("GET", f"/v1/recipe-candidates/{path_part(recipe_id)}")

    async def resume_recipe_candidate(self, recipe_id, *, model=None, max_completion_tokens=None):
        data = _recipe_data(model, max_completion_tokens)
        return await self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/resume", body={"data": data} if data else None)

    async def list_tasks(self, *, limit=20, cursor=None):
        from urllib.parse import urlencode
        return await self._http.request('GET', '/v1/tasks?'+urlencode({'limit':limit, **({'cursor':cursor} if cursor else {})}))

    async def record_feedback(self, revision_id, *, feedback, key):
        return await self._http.request('POST', f'/v1/revisions/{path_part(revision_id)}/feedback', body={'feedback':feedback}, key=stable_key(key))

    async def start_session(self, *, session_id=None):
        return await self._http.request('POST', '/v1/sessions/start', body={'session_id':session_id} if session_id else {})

    async def append_session_events(self, session_id, events):
        return await self._http.request('POST', f'/v1/sessions/{path_part(session_id)}/events', body={'events':events})

    async def get_session_context(self, session_id, *, after=0):
        return await self._http.request('GET', f'/v1/sessions/{path_part(session_id)}/context?after={after}')

    async def end_session(self, session_id):
        return await self._http.request('POST', f'/v1/sessions/{path_part(session_id)}/end')


class SyncWorkflowAtoms:
    """Synchronous counterpart for hosts that already own a sync execution loop."""

    def wait_workflow_job(self, kind, job_id, *, timeout=300):
        getter = _job_getter(self, kind)
        deadline = time.monotonic() + _job_timeout(timeout)
        delay = 2.0
        while True:
            job = getter(job_id)
            status = job.get("status")
            if status not in _JOB_STATES[kind]:
                raise RSIRecoveryRequired(f"Unexpected workflow {kind} status: {status}")
            if status not in {"queued", "running", "awaiting_parent"}:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Workflow {kind} {job_id} is still {status}; resume polling the same ID")
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    def get_artifact(self, artifact_id):
        return self._http.request("GET", f"/v1/artifacts/{path_part(artifact_id)}")

    def create_task(self, *, source, key, objective=None, artifacts=None, self_contained=None):
        return self._http.request("POST", "/v1/tasks", body=_task(source, objective, artifacts, self_contained), key=stable_key(key))

    def get_task(self, task_id):
        return self._http.request("GET", f"/v1/tasks/{path_part(task_id)}")

    def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None, self_contained=None):
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/context",
                                  body=_context(expected_context_version, objective, artifacts, self_contained))


    def start_execution(self, task_id, *, key, recipe_id=None, model=None, include_memory=False):
        data = _model_data(model)
        if type(include_memory) is not bool:
            raise ValueError('include_memory must be boolean')
        if include_memory:
            data['include_memory'] = True
        if recipe_id is not None:
            path_part(recipe_id)
            data['recipe_id'] = recipe_id
        options = {'body': data} if data else {}
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/executions", key=stable_key(key), **options)

    def get_task_recipe(self, task_id):
        return self._http.request('GET', f'/v1/tasks/{path_part(task_id)}/recipe')

    def get_recipe_versions(self, recipe_id):
        return self._http.request('GET', f'/v1/recipe-chains/{path_part(recipe_id)}/versions')


    def get_execution(self, execution_id):
        return self._http.request("GET", f"/v1/executions/{path_part(execution_id)}")

    def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return self._http.request("POST", f"/v1/executions/{path_part(execution_id)}/result",
                                  body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    def get_revision(self, revision_id):
        return self._http.request("GET", f"/v1/revisions/{path_part(revision_id)}")


    def start_revision(self, revision_id, *, key):
        return self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/revisions",
                                  body={"data": {}}, key=stable_key(key))


    def start_recipe_candidate(self, task_id, *, revision_id, key, model=None, max_completion_tokens=None):
        data = _recipe_data(model, max_completion_tokens)
        data.update(revision_id=revision_id)
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/recipe-candidates",
                                  body={"data": data}, key=stable_key(key))

    def get_recipe_candidate(self, recipe_id):
        return self._http.request("GET", f"/v1/recipe-candidates/{path_part(recipe_id)}")

    def resume_recipe_candidate(self, recipe_id, *, model=None, max_completion_tokens=None):
        data = _recipe_data(model, max_completion_tokens)
        return self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/resume", body={"data": data} if data else None)

    def list_tasks(self, *, limit=20, cursor=None):
        from urllib.parse import urlencode
        return self._http.request('GET', '/v1/tasks?'+urlencode({'limit':limit, **({'cursor':cursor} if cursor else {})}))

    def record_feedback(self, revision_id, *, feedback, key):
        return self._http.request('POST', f'/v1/revisions/{path_part(revision_id)}/feedback', body={'feedback':feedback}, key=stable_key(key))

    def start_session(self, *, session_id=None):
        return self._http.request('POST', '/v1/sessions/start', body={'session_id':session_id} if session_id else {})

    def append_session_events(self, session_id, events):
        return self._http.request('POST', f'/v1/sessions/{path_part(session_id)}/events', body={'events':events})

    def get_session_context(self, session_id, *, after=0):
        return self._http.request('GET', f'/v1/sessions/{path_part(session_id)}/context?after={after}')

    def end_session(self, session_id):
        return self._http.request('POST', f'/v1/sessions/{path_part(session_id)}/end')
