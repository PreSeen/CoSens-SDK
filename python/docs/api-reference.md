# Current algorithm API

`CoSensClient.atoms` is async; `.sync_atoms` offers the same operations synchronously. Identity comes from authentication. Each atom sends one request; the paths are the service's own (`/tasks`, `/executions/{id}`, `/recipes`, …) and carry no `/v1` prefix, while the Memory operations keep theirs.

| Operation | Python method |
| --- | --- |
| Artifact upload/read | `artifact`, `get_artifact` |
| Task create/list/read/context | `create_task(key=..., objective=..., artifacts=None)`, `list_tasks`, `get_task`, `supplement_task_context(task_id, expected_context_version=..., objective=None, artifacts=None)` |
| Start an execution | `start_execution(task_id, key=..., source=..., objective=None, artifacts=None, self_contained=None, recipe_id=None, model=None, recall_memory=False)` |
| Start a correction | `start_execution(key=..., previous_execution_id=..., model=None)` |
| Read an execution (its output, feedback and learning) | `get_execution` |
| Submit a result | `submit_execution` |
| Feedback on an execution | `record_feedback(execution_id, feedback=..., source=..., key=...)` |
| Learn a Recipe version from an execution | `learn(execution_id, model=None, max_completion_tokens=None)` |
| Retry a learning | `resume_learning(execution_id, model=None, max_completion_tokens=None)` |
| Wait for a learning | `wait_learning(execution_id, timeout=300)` |
| Recipe versions | `get_recipe_versions` |
| Recipe store | `upload_recipe(recipe_id, version=..., recipe=..., previous_version=None)`, `get_recipe(recipe_id, version=None)`, `list_recipes(limit=20, cursor=None, origin=None)`, `search_recipes(query, limit=10)` |
| Memory | `memory_recall`, `memory_ingest(content=..., request_key=..., ...)`, `get_memory_items`, `get_memory_job`, `get_memory_experience`, `get_memory` |
| Documents | `upload_document`, `create_document_upload_grant`, `upload_document_to_grant`, `get_document_status` |

An execution follows a task; `submit_execution` returns `{task_id, execution_id, status, trace_origin}`. Treat the execution as done only when `status == "completed"`. Feedback, corrections and learning all name the execution.

**Tasks.** A task is a capability, such as writing the weekly management report, that many executions reuse. `create_task` takes the capability (`objective`, reference `artifacts`); `start_execution` takes `source` and this execution's input. A task's artifacts are bare artifact ids or `{"artifact_id": ..., "role": "reference"}`; `create_task` returns `{task_id, objective, artifacts, context_version}`, and `supplement_task_context` changes the objective or the references when `expected_context_version` is still current. `get_task` returns `task_id`, `objective`, `artifacts`, `context_version`, the task's `executions` (each `{execution_id, instance_id, kind, status, request_sha256}`) and its `recipes`, each `{recipe_id, current_version, latest_version, learning}`: `current_version` is the version new executions follow (`None` until the first learning completes), `latest_version` the newest version whatever its learning's status, and `learning` (`{execution_id, version, status, phase, error}`, `None` for an uploaded version) the learning that produced it. A Recipe is being iterated while that learning is `queued`, `running` or `awaiting_previous_version`.

**Executions.** A new execution needs `task_id` and `source` (`new_task` or `external_artifact`) and carries its own input: `objective`, `artifacts` and `self_contained`. Its artifacts are bare ids, which count as `original_input` for `new_task` and as `submitted_output` for `external_artifact`, or `{"artifact_id": ..., "role": ...}` with the role `original_input`, `submitted_output`, `acceptance` or `external_trace`. `ValueError("a new execution needs task_id and source")` and `ValueError("external_artifact requires submitted_output")` are raised before anything is sent. The service answers an input it cannot run with HTTP 422 `input_incomplete`, whose `RSIHTTPError.missing` names what is lacking: `objective`, and for `external_artifact` also `original_input_or_self_contained_task` or `submitted_output`. `recipe_id` must name a Recipe of that task (HTTP 404 otherwise); a task with several Recipes needs one named, and without one the service answers 422 `recipe_choice_required` with the task's Recipes in `RSIHTTPError.recipe_ids`. The frozen request holds the task as it was (`task_context`: `objective`, `artifacts`, `context_version`) and the execution's `input`. The start receipt is `{task_id, execution_id, instance_id, kind, status, request_sha256, request}`; `instance_id`, also on `get_execution`, names the first draft of the execution's correction chain.

**Corrections.** `start_execution(key=..., previous_execution_id=execution_id)` starts a correction of a completed execution. A `task_id` is optional and must be the previous execution's task. It takes only `model` besides; any input field, `recipe_id` or `recall_memory=True` with `previous_execution_id` raises `ValueError("a correction takes only previous_execution_id, task_id and model")` before anything is sent. The correction keeps the previous execution's `task_context`, `input` and `instance_id`, and its request carries the previous output, its feedback and the current version of the Recipe the previous execution followed, or of the task's only Recipe when it followed none (422 `recipe_choice_required` when the task has several).

**Learning.** Call `learn(execution_id)` only after the user confirmed that execution's output. It adds the next version to the Recipe the execution followed, or creates V1 when the task has no Recipe. An execution that followed no Recipe adds to the task's only Recipe; with several, `learn` answers 422 `recipe_choice_required`. An execution learns once: repeating `learn` returns the same learning, and asking it for a different `max_completion_tokens` is rejected with HTTP 422. The next version comes from learning another execution that followed the Recipe: a new execution of the task, or a correction (`previous_execution_id`) of one; learning reads the whole chain of corrections that led to the learned execution. Related Memory is read only when the execution was started with `recall_memory=True`; learning has no Memory parameter. `learn` and `resume_learning` send no idempotency key.

A learning's `status` is `queued`, `running`, `awaiting_previous_version`, `awaiting_runner`, `awaiting_material`, `completed`, `failed` or `interrupted`. `resume_learning` retries a `failed`, `interrupted` or `awaiting_runner` learning and may change its budget; it returns an active or `completed` learning unchanged (a different budget then gets a 422), and answers 422 for an execution that has not learned yet and for an `awaiting_material` learning.

`wait_learning(execution_id, timeout=300)` polls `get_execution`, waiting 2 s and doubling up to 30 s, and never starts or resumes a learning:

- `completed`: returns the learning.
- `failed`: raises `RSILearningFailed` with `execution_id`, `code`, `message`, `retryable`, `phase`, `upstream_status`, `upstream_code` and `request_id`. `retryable` is `True` when `resume_learning` may succeed, `False` when it will not, and `None` when the learning does not say (a bare code such as `previous_version_failed` has no detail and can still be resumed).
- `interrupted`, `awaiting_runner` or `awaiting_material`: raises `RSILearningStopped` with `learning`, `execution_id`, `status` and `request_id`. These are not failures: nothing changes until the caller acts. Resume an `interrupted` learning, resume an `awaiting_runner` one with a `model`, and for `awaiting_material` (the service kept the current version; `material_gap` says why) give feedback or evidence in a correction.
- still `queued`, `running` or `awaiting_previous_version` at the deadline: raises `TimeoutError`; call it again.
- no learning, or an unknown status: raises `RSIRecoveryRequired`.

`record_feedback` requires `source`: `human`, `evaluator`, `reference`, `automated_check` or `downstream`, and is accepted only for a completed execution. Feedback attribution happens inside learning. A failed learning does not replace the usable current version.

`submit_execution` takes the exact frozen request SHA, actual outcome, artifacts and optional events. Stable keys and event IDs are required for reliable retries. The public model ID is supplied with `model`; a task's model is frozen once set. Model calls reuse the existing Authorization API Key, with the gateway URL and auth type determined internally. There is no `model_api` connection object or second credential. Every returned JSON object exposes the response's `x-request-id` as `.request_id`.

`upload_recipe` stores a Recipe learned elsewhere; `previous_version` is the version it builds on. `list_recipes(origin=...)` filters by `uploaded` or `generated`; only a `generated` Recipe can be applied to an execution with `recipe_id`.

Current HTTP and MCP schemas are supplied by the algorithm service. Old Run, Revision, Recipe candidate, Session, attribution, acceptance, rubric and learning-job operations are absent.

### Recipe completion budget

`learn(..., max_completion_tokens=200000)` and `resume_learning(..., max_completion_tokens=300000)` accept an optional integer from 1 through 1,000,000. Omitting it leaves the budget to the service (32,000 by default). The budget is frozen with the learning: a resume keeps it unless explicitly overridden. The same budget applies to both feedback attribution and Recipe creation/update. Model/gateway limits still apply; an accepted SDK value is not a promise that every upstream model supports it.

The wrappers expose the same option on `client.cosens.generate_recipe(turn_id, max_completion_tokens=200000)`; the transparent wrapper also has `client.cosens.resume_learning(turn_id, max_completion_tokens=300000)`. It does not change business response limits or `decision_max_output_tokens`. Asking a turn that already learned for a different budget is rejected; resume a failed, interrupted or awaiting_runner learning to change it.

## Local RSI

See [Local RSI](local-rsi.md). `LocalRSI` takes and returns the task and new-execution shapes above, and keeps its earlier in-process names for corrections and learning (`start_revision`, `start_recipe_candidate`, `wait_workflow_job`, …). Its evidence references now use `execution_id`.
