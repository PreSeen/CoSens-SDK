"""HTTP atoms for the task / execution / Recipe workflow.

These methods send exactly one request. Agent execution, consent and retries belong
to the caller; in particular a lost artifact receipt must not trigger re-upload.
"""

import asyncio
import json
import math
import re
import time
from urllib.parse import urlencode

from .atoms_helpers import path_part, stable_key
from .errors import RSILearningFailed, RSILearningStopped, RSIRecoveryRequired


# Where a feedback text comes from; the service weighs feedback by its source.
FEEDBACK_SOURCES = ('human', 'evaluator', 'reference', 'automated_check', 'downstream')


def feedback_source(source):
    if source not in FEEDBACK_SOURCES:
        raise ValueError('source must be one of ' + ', '.join(FEEDBACK_SOURCES))
    return source


def _model_data(model):
    if model is None:
        return {}
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError('model must be a public model ID')
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
# Learning states the service moves on by itself, and states only the caller can change.
_LEARNING_IN_FLIGHT = {"queued", "running", "awaiting_previous_version"}
_LEARNING_STOPPED = {"interrupted", "awaiting_runner", "awaiting_material"}


def _job_timeout(timeout):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite nonnegative number")
    return timeout


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


_RECIPE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _positive(value, name):
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _recipe_upload(recipe_id, version, recipe, previous_version):
    if not isinstance(recipe_id, str) or not _RECIPE_ID.fullmatch(recipe_id):
        raise ValueError("recipe_id must be 1-128 letters, digits, '_' or '-'")
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be an object")
    _positive(version, "version")
    body = {}
    if previous_version is not None:
        body["previous_version"] = _positive(previous_version, "previous_version")
    body["recipe"] = recipe
    return body


def _recipe_path(recipe_id, version):
    suffix = "?" + urlencode({"version": _positive(version, "version")}) if version is not None else ""
    return f"/recipes/{path_part(recipe_id)}" + suffix


def _recipes_list_path(limit, cursor, origin):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    params = {"limit": limit}
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor.strip():
            raise ValueError("cursor must be a nonempty string")
        params["cursor"] = cursor
    if origin is not None:
        if origin not in ("uploaded", "generated"):
            raise ValueError("origin must be uploaded or generated")
        params["origin"] = origin
    return "/recipes?" + urlencode(params)


def _recipes_search_path(query, limit):
    if not isinstance(query, str) or not 1 <= len(query) <= 1000:
        raise ValueError("query must be 1-1000 characters")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be an integer from 1 to 50")
    return "/recipes?" + urlencode({"q": query, "limit": limit})


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


def _execution_start(recipe_id, model, recall_memory, previous_execution_id):
    data = _model_data(model)
    if type(recall_memory) is not bool:
        raise ValueError('recall_memory must be boolean')
    if previous_execution_id is not None:
        if recipe_id is not None or recall_memory:
            raise ValueError("a correction takes only previous_execution_id and model")
        path_part(previous_execution_id)
        data['previous_execution_id'] = previous_execution_id
    if recall_memory:
        data['recall_memory'] = True
    if recipe_id is not None:
        path_part(recipe_id)
        data['recipe_id'] = recipe_id
    return {'body': data} if data else {}


def _waited_learning(execution, execution_id):
    """The learning when waiting is over, None while it is in flight; raises on failure or a stop."""
    learning = execution.get("learning")
    if not isinstance(learning, dict):
        raise RSIRecoveryRequired(f"Execution {execution_id} has no learning; call learn first")
    status = learning.get("status")
    request_id = getattr(execution, "request_id", None)
    if status == "completed":
        return learning
    if status == "failed":
        raise RSILearningFailed.from_learning(learning, request_id=request_id)
    if status in _LEARNING_STOPPED:
        raise RSILearningStopped(learning, request_id=request_id)
    if status not in _LEARNING_IN_FLIGHT:
        raise RSIRecoveryRequired(f"Unexpected learning status: {status}")
    return None


class AsyncWorkflowAtoms:
    async def get_artifact(self, artifact_id):
        return await self._http.request("GET", f"/artifacts/{path_part(artifact_id)}")

    async def create_task(self, *, source, key, objective=None, artifacts=None, self_contained=None):
        return await self._http.request("POST", "/tasks", body=_task(source, objective, artifacts, self_contained), key=stable_key(key))

    async def get_task(self, task_id):
        return await self._http.request("GET", f"/tasks/{path_part(task_id)}")

    async def list_tasks(self, *, limit=20, cursor=None):
        return await self._http.request('GET', '/tasks?'+urlencode({'limit':limit, **({'cursor':cursor} if cursor else {})}))

    async def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None, self_contained=None):
        return await self._http.request("POST", f"/tasks/{path_part(task_id)}/context",
                                        body=_context(expected_context_version, objective, artifacts, self_contained))

    async def start_execution(self, task_id, *, key, recipe_id=None, model=None, recall_memory=False, previous_execution_id=None):
        """Start an execution; with ``previous_execution_id`` it is a correction of that execution."""
        options = _execution_start(recipe_id, model, recall_memory, previous_execution_id)
        return await self._http.request("POST", f"/tasks/{path_part(task_id)}/executions", key=stable_key(key), **options)

    async def get_execution(self, execution_id):
        return await self._http.request("GET", f"/executions/{path_part(execution_id)}")

    async def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return await self._http.request("POST", f"/executions/{path_part(execution_id)}/result",
                                        body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    async def record_feedback(self, execution_id, *, feedback, source, key):
        return await self._http.request('POST', f'/executions/{path_part(execution_id)}/feedback',
                                        body={'feedback':feedback, 'source':feedback_source(source)}, key=stable_key(key))

    async def learn(self, execution_id, *, model=None, max_completion_tokens=None):
        """Learn a Recipe version from a confirmed execution; repeat calls return the same learning."""
        return await self._http.request("POST", f"/executions/{path_part(execution_id)}/learn",
                                        body=_recipe_data(model, max_completion_tokens) or {})

    async def resume_learning(self, execution_id, *, model=None, max_completion_tokens=None):
        return await self._http.request("POST", f"/executions/{path_part(execution_id)}/resume",
                                        body=_recipe_data(model, max_completion_tokens) or None)

    async def wait_learning(self, execution_id, *, timeout=300):
        """Poll the execution until its learning is ``completed`` and return the learning.

        Raises ``RSILearningFailed`` on ``failed``, ``RSILearningStopped`` on
        ``interrupted``, ``awaiting_runner`` or ``awaiting_material``, and
        ``TimeoutError`` while it is still in flight; never starts or resumes learning.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _job_timeout(timeout)
        delay = 2.0
        while True:
            execution = await self.get_execution(execution_id)
            learning = _waited_learning(execution, execution_id)
            if learning is not None:
                return learning
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Learning for execution {execution_id} is still {execution['learning']['status']}; call wait_learning again")
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    async def get_recipe_versions(self, recipe_id):
        return await self._http.request('GET', f'/recipes/{path_part(recipe_id)}/versions')

    async def upload_recipe(self, recipe_id, *, version, recipe, previous_version=None):
        """Store a Recipe version learned outside the service; ``previous_version`` is the version it was built on."""
        body = _recipe_upload(recipe_id, version, recipe, previous_version)
        return await self._http.request("PUT", f"/recipes/{path_part(recipe_id)}/versions/{version}", body=body)

    async def get_recipe(self, recipe_id, *, version=None):
        return await self._http.request("GET", _recipe_path(recipe_id, version))

    async def list_recipes(self, *, limit=20, cursor=None, origin=None):
        return await self._http.request("GET", _recipes_list_path(limit, cursor, origin))

    async def search_recipes(self, query, *, limit=10):
        return await self._http.request("GET", _recipes_search_path(query, limit))


class SyncWorkflowAtoms:
    """Synchronous counterpart for hosts that already own a sync execution loop."""

    def get_artifact(self, artifact_id):
        return self._http.request("GET", f"/artifacts/{path_part(artifact_id)}")

    def create_task(self, *, source, key, objective=None, artifacts=None, self_contained=None):
        return self._http.request("POST", "/tasks", body=_task(source, objective, artifacts, self_contained), key=stable_key(key))

    def get_task(self, task_id):
        return self._http.request("GET", f"/tasks/{path_part(task_id)}")

    def list_tasks(self, *, limit=20, cursor=None):
        return self._http.request('GET', '/tasks?'+urlencode({'limit':limit, **({'cursor':cursor} if cursor else {})}))

    def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None, self_contained=None):
        return self._http.request("POST", f"/tasks/{path_part(task_id)}/context",
                                  body=_context(expected_context_version, objective, artifacts, self_contained))

    def start_execution(self, task_id, *, key, recipe_id=None, model=None, recall_memory=False, previous_execution_id=None):
        options = _execution_start(recipe_id, model, recall_memory, previous_execution_id)
        return self._http.request("POST", f"/tasks/{path_part(task_id)}/executions", key=stable_key(key), **options)

    def get_execution(self, execution_id):
        return self._http.request("GET", f"/executions/{path_part(execution_id)}")

    def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return self._http.request("POST", f"/executions/{path_part(execution_id)}/result",
                                  body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    def record_feedback(self, execution_id, *, feedback, source, key):
        return self._http.request('POST', f'/executions/{path_part(execution_id)}/feedback',
                                  body={'feedback':feedback, 'source':feedback_source(source)}, key=stable_key(key))

    def learn(self, execution_id, *, model=None, max_completion_tokens=None):
        return self._http.request("POST", f"/executions/{path_part(execution_id)}/learn",
                                  body=_recipe_data(model, max_completion_tokens) or {})

    def resume_learning(self, execution_id, *, model=None, max_completion_tokens=None):
        return self._http.request("POST", f"/executions/{path_part(execution_id)}/resume",
                                  body=_recipe_data(model, max_completion_tokens) or None)

    def wait_learning(self, execution_id, *, timeout=300):
        deadline = time.monotonic() + _job_timeout(timeout)
        delay = 2.0
        while True:
            execution = self.get_execution(execution_id)
            learning = _waited_learning(execution, execution_id)
            if learning is not None:
                return learning
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Learning for execution {execution_id} is still {execution['learning']['status']}; call wait_learning again")
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    def get_recipe_versions(self, recipe_id):
        return self._http.request('GET', f'/recipes/{path_part(recipe_id)}/versions')

    def upload_recipe(self, recipe_id, *, version, recipe, previous_version=None):
        body = _recipe_upload(recipe_id, version, recipe, previous_version)
        return self._http.request("PUT", f"/recipes/{path_part(recipe_id)}/versions/{version}", body=body)

    def get_recipe(self, recipe_id, *, version=None):
        return self._http.request("GET", _recipe_path(recipe_id, version))

    def list_recipes(self, *, limit=20, cursor=None, origin=None):
        return self._http.request("GET", _recipes_list_path(limit, cursor, origin))

    def search_recipes(self, query, *, limit=10):
        return self._http.request("GET", _recipes_search_path(query, limit))
