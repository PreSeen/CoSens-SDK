"""Explicit host-coordinated entry for the new task workflow.

The returned execution request is a hand-off to the host. This function never
executes an Agent, accepts an output implicitly, or retries an unknown effect.
"""

import hashlib
import inspect

from .atoms_helpers import stable_key
from .errors import RSIApprovalUnavailable, RSIApprovalRequired, RSIRecoveryRequired
from .models import result


def _derived_key(key, operation):
    return hashlib.sha256(f"workflow-start:{operation}:{stable_key(key)}".encode()).hexdigest()


async def start_workflow(atoms, session_context, *, phase="prepare", mode="auto", request_key=None, authorize=None):
    if mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval or disable")
    if mode == "disable":
        return result({"status": "disabled", "phase": phase})
    if not isinstance(session_context, dict):
        raise TypeError("session_context must be an explicitly authorized snapshot object")
    if mode == "approval":
        if authorize is None:
            raise RSIApprovalUnavailable("approval mode requires a host authorization callback")
        allowed = authorize(phase, session_context)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            raise RSIApprovalRequired("host authorization was not granted")

    if phase == "prepare":
        stable_key(request_key)
        task_id = session_context.get("task_id")
        if task_id is None:
            task_data = session_context.get("task")
            if not isinstance(task_data, dict):
                raise ValueError("prepare requires task or task_id")
            task = await atoms.create_task(source=task_data["source"], objective=task_data.get("objective"),
                                           artifacts=task_data.get("artifacts"), self_contained=task_data.get("self_contained"),
                                           key=_derived_key(request_key, "task"))
            task_id = task.get("task_id")
            if task.get("status") == "ready" and not task_id:
                raise RSIRecoveryRequired("Ready task receipt has no task_id")
            if task.get("status") != "ready":
                return result({"phase": phase, "task": task, "status": task.get("status")})
        else:
            task = await atoms.get_task(task_id)
            if task.get("status") != "ready":
                return result({"phase": phase, "task": task, "status": task.get("status")})
        execution = await atoms.start_execution(task_id, key=_derived_key(request_key, "execution"))
        return result({"phase": phase, "status": execution.get("status"), "task_id": task_id,
                       "task": task, "execution_id": execution.get("execution_id"),
                       "request_sha256": execution.get("request_sha256"), "execution_request": execution.get("request"),
                       "execution": execution})

    stable_key(request_key)
    if phase == "analyze":
        return await atoms.start_external_analysis(session_context["task_id"], key=request_key)
    if phase == "attribute":
        return await atoms.start_attribution(session_context["revision_id"], feedback=session_context.get("feedback"), key=request_key)
    if phase == "revise":
        return await atoms.start_revision(session_context["revision_id"], attribution_id=session_context["attribution_id"], key=request_key)
    if phase == "recipe":
        return await atoms.start_recipe_candidate(session_context["task_id"], revision_id=session_context["revision_id"],
                                                  include_memory=session_context.get("include_memory", False), key=request_key)
    if phase == "verify":
        return await atoms.start_verification(session_context["recipe_candidate_id"], key=request_key)
    raise ValueError("phase must be prepare, analyze, attribute, revise, recipe or verify")
