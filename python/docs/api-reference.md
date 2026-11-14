# Current algorithm API

`CoSensClient.atoms` is async; `.sync_atoms` offers the same operations synchronously. Identity comes from authentication. Each atom sends one request; the paths are the service's own (`/tasks`, `/executions/{id}`, `/recipes`, …) and carry no `/v1` prefix, while the Memory operations keep theirs.

| Operation | Python method |
| --- | --- |
| Artifact upload/read | `artifact`, `get_artifact` |
| Task create/list/read/context | `create_task`, `list_tasks`, `get_task`, `supplement_task_context` |
| Start an execution or a correction | `start_execution(task_id, key=..., recipe_id=None, model=None, recall_memory=False, previous_execution_id=None)` |
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

**Corrections.** `start_execution(task_id, key=..., previous_execution_id=execution_id)` starts a correction of a completed execution. It takes only `model` besides; `recipe_id` or `recall_memory=True` with `previous_execution_id` raises `ValueError` before anything is sent. The correction's request carries the previous output, its feedback and the task's current Recipe.

**Learning.** Call `learn(execution_id)` only after the user confirmed that execution's output. It creates V1 when the task has no Recipe, otherwise the next version. An execution learns once: repeating `learn` returns the same learning, and asking it for a different `max_completion_tokens` is rejected with HTTP 422. Learning the next version means a new execution (a correction), and learning reads the whole chain of executions. Related Memory is read only when the execution was started with `recall_memory=True`; learning has no Memory parameter. `learn` and `resume_learning` send no idempotency key.

A learning's `status` is `queued`, `running`, `awaiting_previous_version`, `awaiting_runner`, `awaiting_material`, `completed`, `failed` or `interrupted`. `resume_learning` retries a `failed`, `interrupted` or `awaiting_runner` learning and may change its budget; it returns an active or `completed` learning unchanged (a different budget then gets a 422), and answers 422 for an execution that has not learned yet and for an `awaiting_material` learning.

`wait_learning(execution_id, timeout=300)` polls `get_execution`, waiting 2 s and doubling up to 30 s, and never starts or resumes a learning:

- `completed`: returns the learning.
- `failed`: raises `RSILearningFailed` with `execution_id`, `code`, `message`, `retryable`, `phase`, `upstream_status`, `upstream_code` and `request_id`. `retryable` is `True` when `resume_learning` may succeed, `False` when it will not, and `None` when the learning does not say (a bare code such as `previous_version_failed` has no detail and can still be resumed).
- `interrupted`, `awaiting_runner` or `awaiting_material`: raises `RSILearningStopped` with `learning`, `execution_id`, `status` and `request_id`. These are not failures: nothing changes until the caller acts. Resume an `interrupted` learning, resume an `awaiting_runner` one with a `model`, and for `awaiting_material` (the service kept the current version; `material_gap` says why) give feedback or evidence in a correction.
- still `queued`, `running` or `awaiting_previous_version` at the deadline: raises `TimeoutError`; call it again.
- no learning, or an unknown status: raises `RSIRecoveryRequired`.

`record_feedback` requires `source`: `human`, `evaluator`, `reference`, `automated_check` or `downstream`, and is accepted only for a completed execution. Feedback attribution happens inside learning. A failed learning does not replace the usable current version.

`create_task` requires `source="new_task"` or `source="external_artifact"`. `submit_execution` takes the exact frozen request SHA, actual outcome, artifacts and optional events. Stable keys and event IDs are required for reliable retries. The public model ID is supplied with `model`; a task's model is frozen once set. Model calls reuse the existing Authorization API Key, with the gateway URL and auth type determined internally. There is no `model_api` connection object or second credential. Every returned JSON object exposes the response's `x-request-id` as `.request_id`.

`upload_recipe` stores a Recipe learned elsewhere; `previous_version` is the version it builds on. `list_recipes(origin=...)` filters by `uploaded` or `generated`; only a `generated` Recipe can be applied to an execution with `recipe_id`.

Current HTTP and MCP schemas are supplied by the algorithm service. Old Run, Revision, Recipe candidate, Session, attribution, acceptance, rubric and learning-job operations are absent.

### Recipe completion budget

`learn(..., max_completion_tokens=200000)` and `resume_learning(..., max_completion_tokens=300000)` accept an optional integer from 1 through 1,000,000. Omitting it leaves the budget to the service (32,000 by default). The budget is frozen with the learning: a resume keeps it unless explicitly overridden. The same budget applies to both feedback attribution and Recipe creation/update. Model/gateway limits still apply; an accepted SDK value is not a promise that every upstream model supports it.

The wrappers expose the same option on `client.cosens.generate_recipe(turn_id, max_completion_tokens=200000)`; the transparent wrapper also has `client.cosens.resume_learning(turn_id, max_completion_tokens=300000)`. It does not change business response limits or `decision_max_output_tokens`. Asking a turn that already learned for a different budget is rejected; resume a failed, interrupted or awaiting_runner learning to change it.

## Local RSI

See [Local RSI](local-rsi.md). `LocalRSI` keeps its earlier in-process workflow names; it does not yet follow the execution API. Its evidence references now use `execution_id`.
