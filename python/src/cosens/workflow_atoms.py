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


_ROLES = {"original_input", "submitted_output", "acceptance", "external_trace"}
_JOB_STATES = {
    "analysis": {"queued", "running", "awaiting_runner", "completed", "failed"},
    "attribution": {"queued", "running", "awaiting_runner", "awaiting_selection", "inconclusive", "selected", "failed"},
    "recipe_candidate": {"queued", "running", "awaiting_runner", "candidate", "failed"},
}


def _job_timeout(timeout):
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("timeout must be a finite nonnegative number")
    return timeout


def _job_getter(owner, kind):
    names = {"analysis": "get_external_analysis", "attribution": "get_attribution", "recipe_candidate": "get_recipe_candidate"}
    if kind not in names:
        raise ValueError("job kind must be analysis, attribution or recipe_candidate")
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


def _selection(candidate_sha256, candidate_id, custom_reason):
    if bool(candidate_id) == bool(custom_reason):
        raise ValueError("provide exactly one candidate_id or custom_reason")
    if not isinstance(candidate_sha256, str) or not candidate_sha256.strip():
        raise ValueError("candidate_sha256 is required")
    if candidate_id is not None and (not isinstance(candidate_id, str) or not candidate_id.strip()):
        raise ValueError("candidate_id must be nonempty")
    if custom_reason is not None and (not isinstance(custom_reason, str) or not custom_reason.strip() or len(custom_reason) > 4_000):
        raise ValueError("custom_reason must be nonempty text of at most 4,000 characters")
    data = {"candidate_sha256": candidate_sha256}
    if candidate_id:
        data["candidate_id"] = candidate_id
    else:
        data["custom_reason"] = custom_reason
    return {"data": data}


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
            if status not in {"queued", "running"}:
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

    async def start_external_analysis(self, task_id, *, key):
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/analysis", key=stable_key(key))

    async def get_external_analysis(self, analysis_id):
        return await self._http.request("GET", f"/v1/task-analyses/{path_part(analysis_id)}")

    async def resume_external_analysis(self, analysis_id):
        return await self._http.request("POST", f"/v1/task-analyses/{path_part(analysis_id)}/resume")

    async def start_execution(self, task_id, *, key):
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/executions", key=stable_key(key))

    async def start_reconstruction(self, task_id, *, key):
        """Compatibility route for external_artifact tasks; shares execution key space."""
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/reconstructions", key=stable_key(key))

    async def get_execution(self, execution_id):
        return await self._http.request("GET", f"/v1/executions/{path_part(execution_id)}")

    async def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return await self._http.request("POST", f"/v1/executions/{path_part(execution_id)}/result",
                                        body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    async def get_revision(self, revision_id):
        return await self._http.request("GET", f"/v1/revisions/{path_part(revision_id)}")

    async def start_attribution(self, revision_id, *, key, feedback=None):
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4_000):
            raise ValueError("feedback must be nonempty text of at most 4,000 characters")
        data = {} if feedback is None else {"feedback": feedback}
        return await self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/attributions",
                                        body={"data": data}, key=stable_key(key))

    async def get_attribution(self, attribution_id):
        return await self._http.request("GET", f"/v1/attributions/{path_part(attribution_id)}")

    async def resume_attribution(self, attribution_id):
        return await self._http.request("POST", f"/v1/attributions/{path_part(attribution_id)}/resume")

    async def select_cause(self, attribution_id, *, candidate_sha256, candidate_id=None, custom_reason=None):
        return await self._http.request("POST", f"/v1/attributions/{path_part(attribution_id)}/selection",
                                        body=_selection(candidate_sha256, candidate_id, custom_reason))

    async def start_revision(self, revision_id, *, attribution_id, key):
        return await self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/revisions",
                                        body={"data": {"attribution_id": attribution_id}}, key=stable_key(key))

    async def accept_revision(self, revision_id, *, artifact_hashes):
        return await self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/acceptance",
                                        body={"data": {"artifact_hashes": artifact_hashes}})

    async def start_recipe_candidate(self, task_id, *, revision_id, key, include_memory=False):
        return await self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/recipe-candidates",
                                        body={"data": {"revision_id": revision_id, "include_memory": include_memory}}, key=stable_key(key))

    async def get_recipe_candidate(self, recipe_id):
        return await self._http.request("GET", f"/v1/recipe-candidates/{path_part(recipe_id)}")

    async def resume_recipe_candidate(self, recipe_id):
        return await self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/resume")

    async def start_verification(self, recipe_id, *, key):
        return await self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/verifications", key=stable_key(key))

    async def get_verification(self, verification_id):
        return await self._http.request("GET", f"/v1/recipe-verifications/{path_part(verification_id)}")


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
            if status not in {"queued", "running"}:
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

    def start_external_analysis(self, task_id, *, key):
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/analysis", key=stable_key(key))

    def get_external_analysis(self, analysis_id):
        return self._http.request("GET", f"/v1/task-analyses/{path_part(analysis_id)}")

    def resume_external_analysis(self, analysis_id):
        return self._http.request("POST", f"/v1/task-analyses/{path_part(analysis_id)}/resume")

    def start_execution(self, task_id, *, key):
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/executions", key=stable_key(key))

    def start_reconstruction(self, task_id, *, key):
        """Compatibility route for external_artifact tasks; shares execution key space."""
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/reconstructions", key=stable_key(key))

    def get_execution(self, execution_id):
        return self._http.request("GET", f"/v1/executions/{path_part(execution_id)}")

    def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        return self._http.request("POST", f"/v1/executions/{path_part(execution_id)}/result",
                                  body=_result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage))

    def get_revision(self, revision_id):
        return self._http.request("GET", f"/v1/revisions/{path_part(revision_id)}")

    def start_attribution(self, revision_id, *, key, feedback=None):
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4_000):
            raise ValueError("feedback must be nonempty text of at most 4,000 characters")
        data = {} if feedback is None else {"feedback": feedback}
        return self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/attributions",
                                  body={"data": data}, key=stable_key(key))

    def get_attribution(self, attribution_id):
        return self._http.request("GET", f"/v1/attributions/{path_part(attribution_id)}")

    def resume_attribution(self, attribution_id):
        return self._http.request("POST", f"/v1/attributions/{path_part(attribution_id)}/resume")

    def select_cause(self, attribution_id, *, candidate_sha256, candidate_id=None, custom_reason=None):
        return self._http.request("POST", f"/v1/attributions/{path_part(attribution_id)}/selection",
                                  body=_selection(candidate_sha256, candidate_id, custom_reason))

    def start_revision(self, revision_id, *, attribution_id, key):
        return self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/revisions",
                                  body={"data": {"attribution_id": attribution_id}}, key=stable_key(key))

    def accept_revision(self, revision_id, *, artifact_hashes):
        return self._http.request("POST", f"/v1/revisions/{path_part(revision_id)}/acceptance",
                                  body={"data": {"artifact_hashes": artifact_hashes}})

    def start_recipe_candidate(self, task_id, *, revision_id, key, include_memory=False):
        return self._http.request("POST", f"/v1/tasks/{path_part(task_id)}/recipe-candidates",
                                  body={"data": {"revision_id": revision_id, "include_memory": include_memory}}, key=stable_key(key))

    def get_recipe_candidate(self, recipe_id):
        return self._http.request("GET", f"/v1/recipe-candidates/{path_part(recipe_id)}")

    def resume_recipe_candidate(self, recipe_id):
        return self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/resume")

    def start_verification(self, recipe_id, *, key):
        return self._http.request("POST", f"/v1/recipe-candidates/{path_part(recipe_id)}/verifications", key=stable_key(key))

    def get_verification(self, verification_id):
        return self._http.request("GET", f"/v1/recipe-verifications/{path_part(verification_id)}")
