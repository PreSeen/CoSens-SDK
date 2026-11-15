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


async def start_workflow(atoms, session_context, *, phase="prepare", mode="auto", request_key=None, authorize=None, recipe_id=None, model=None, recall_memory=False):
    if mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval or disable")
    if mode == "disable":
        return result({"status": "disabled", "phase": phase})
    if type(recall_memory) is not bool:
        raise ValueError('recall_memory must be boolean')
    if isinstance(session_context, str) and phase == 'prepare':
        if not session_context.strip():
            raise ValueError('task Prompt must be nonempty')
        session_context = {'task': {'source': 'new_task', 'objective': session_context}}
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
        task_data = session_context.get("task")
        if not isinstance(task_data, dict):
            raise ValueError("prepare requires task: the execution's source, objective and artifacts")
        task_id = session_context.get("task_id")
        if task_id is None:
            task = await atoms.create_task(objective=task_data.get("objective"), key=_derived_key(request_key, "task"))
            task_id = task.get("task_id")
            if not task_id:
                raise RSIRecoveryRequired("Task receipt has no task_id")
        else:
            task = await atoms.get_task(task_id)
        options = {}
        if recipe_id is not None:
            options['recipe_id'] = recipe_id
        if model is not None:
            options['model'] = model
        if recall_memory:
            options['recall_memory'] = True
        execution = await atoms.start_execution(
            task_id, key=_derived_key(request_key, "execution"), source=task_data.get("source"),
            objective=task_data.get("objective"), artifacts=task_data.get("artifacts"),
            self_contained=task_data.get("self_contained"), **options)
        return result({"phase": phase, "status": execution.get("status"), "task_id": task_id,
                       "task": task, "execution_id": execution.get("execution_id"),
                       "request_sha256": execution.get("request_sha256"), "execution_request": execution.get("request"),
                       "execution": execution})

    if phase == 'correct':
        # A correction of a completed execution takes only the execution it corrects and a model.
        if recipe_id is not None or recall_memory:
            raise ValueError("a correction takes only previous_execution_id, task_id and model")
        return await atoms.start_execution(session_context.get('task_id'), key=stable_key(request_key),
                                           previous_execution_id=session_context['execution_id'],
                                           **({'model': model} if model is not None else {}))
    if phase == 'learn':
        return await atoms.learn(session_context['execution_id'], **({'model': model} if model is not None else {}))
    raise ValueError('phase must be prepare, correct or learn')
