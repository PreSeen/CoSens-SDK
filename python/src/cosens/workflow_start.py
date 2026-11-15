"""Explicit host-coordinated entry for the new task workflow.

The returned execution request is a hand-off to the host. This function never
executes an Agent, accepts an output implicitly, or retries an unknown effect.
"""

import hashlib
import inspect

from .atoms_helpers import path_part, stable_key
from .errors import RSIApprovalUnavailable, RSIApprovalRequired, RSIRecoveryRequired
from .models import result
from .workflow_atoms import _input, _model_data, _objective


def _derived_key(key, operation):
    return hashlib.sha256(f"workflow-start:{operation}:{stable_key(key)}".encode()).hexdigest()


def _execution_options(task_data, *, creates_task, recipe_id=None, model=None, recall_memory=False):
    """start_execution's arguments for a task spec, checked before any task is created so a bad spec leaves none."""
    if not isinstance(task_data, dict):
        raise ValueError("prepare requires task: the execution's source, objective and artifacts")
    if task_data.get("source") is None:
        raise ValueError("task.source is required")
    if creates_task:
        _objective(task_data.get("objective"))
    elif model is not None:
        # The service freezes a task's model, so every version of its Recipes stays on one model.
        raise ValueError("model names the model of a task the prepare creates; an existing task keeps its own")
    options = {"source": task_data["source"], "objective": task_data.get("objective"),
               "artifacts": task_data.get("artifacts"), "self_contained": task_data.get("self_contained")}
    _input(**options)
    options.update(_model_data(model))
    if recipe_id is not None:
        path_part(recipe_id)
        options["recipe_id"] = recipe_id
    if recall_memory:
        options["recall_memory"] = True
    return options


async def _recipe_task(atoms, recipe_id, task_id):
    """The task a Recipe belongs to; its executions run there and nowhere else."""
    owner = (await atoms.get_recipe(recipe_id)).get("task_id")
    if not owner:
        raise ValueError(f"Recipe {recipe_id} belongs to no task")
    if task_id is not None and task_id != owner:
        raise ValueError(f"Recipe {recipe_id} belongs to task {owner}, not {task_id}")
    return owner


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
    # Arguments are checked before the host is asked, so a refusal never follows an approval.
    if phase == "prepare":
        stable_key(request_key)
        task_data = session_context.get("task")
        options = _execution_options(task_data, creates_task=session_context.get("task_id") is None and recipe_id is None,
                                     recipe_id=recipe_id, model=model, recall_memory=recall_memory)
        if recipe_id is None and session_context.get("task_id") is not None:
            path_part(session_context["task_id"])
        if recipe_id is not None:
            # The host approves the task the Recipe's execution actually runs in.
            session_context = {**session_context,
                               "task_id": await _recipe_task(atoms, recipe_id, session_context.get("task_id"))}
    elif phase == 'correct':
        # A correction of a completed execution takes only the execution it corrects and a model.
        if recipe_id is not None or recall_memory:
            raise ValueError("a correction takes only previous_execution_id, task_id and model")
        stable_key(request_key)
        path_part(session_context.get('execution_id'))
        if session_context.get('task_id') is not None:
            path_part(session_context['task_id'])
        _model_data(model)
    if mode == "approval":
        if authorize is None:
            raise RSIApprovalUnavailable("approval mode requires a host authorization callback")
        allowed = authorize(phase, session_context)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            raise RSIApprovalRequired("host authorization was not granted")

    if phase == "prepare":
        task_id = session_context.get("task_id")
        if task_id is None:
            task = await atoms.create_task(objective=task_data["objective"], key=_derived_key(request_key, "task"))
            task_id = task.get("task_id")
            if not task_id:
                raise RSIRecoveryRequired("Task receipt has no task_id")
        else:
            task = await atoms.get_task(task_id)
        execution = await atoms.start_execution(task_id, key=_derived_key(request_key, "execution"), **options)
        return result({"phase": phase, "status": execution.get("status"), "task_id": task_id,
                       "task": task, "execution_id": execution.get("execution_id"),
                       "request_sha256": execution.get("request_sha256"), "execution_request": execution.get("request"),
                       "execution": execution})

    if phase == 'correct':
        return await atoms.start_execution(session_context.get('task_id'), key=stable_key(request_key),
                                           previous_execution_id=session_context['execution_id'],
                                           **({'model': model} if model is not None else {}))
    if phase == 'learn':
        return await atoms.learn(session_context['execution_id'], **({'model': model} if model is not None else {}))
    raise ValueError('phase must be prepare, correct or learn')
