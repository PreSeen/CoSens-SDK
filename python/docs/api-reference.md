# Current algorithm API

`CoSensClient.atoms` is async; `.sync_atoms` offers the same operations synchronously. Identity comes from authentication.

| Operation | Python method |
| --- | --- |
| Artifact upload/read | `artifact`, `get_artifact` |
| Task create/list/read/context | `create_task`, `list_tasks`, `get_task`, `supplement_task_context` |
| Prepare/read execution | `start_execution`, `get_execution` |
| Submit/read revision | `submit_execution`, `get_revision` |
| Feedback/correction | `record_feedback`, `start_revision` |
| Explicit Recipe generation/update | `start_recipe_candidate(task_id, revision_id=..., key=None, model=..., max_completion_tokens=...)` |
| Read/list/resume generation | `get_recipe_candidate`, `list_recipe_candidates`, `resume_recipe_candidate` |
| Associated Recipe/version chain | `get_task_recipe`, `get_recipe_versions` |
| Poll generation | `wait_workflow_job("recipe_candidate", id)` (raises `RSICandidateFailed` on failure) |
| Sessions | `start_session`, `append_session_events`, `get_session_context`, `end_session` |
| Memory | `user_get`, `memory_recall`, `memory_get`, `timeline_get`, `memory_write`, `memory_delete`, `document_upload` |

Call generation only after external artifact confirmation. It creates V1 if no Recipe exists, otherwise a new version. Related Memory is read only when the execution behind the revision was started with `recall_memory=True`; generation has no Memory parameter. `record_feedback` requires `source`: `human`, `evaluator`, `reference`, `automated_check` or `downstream`. Feedback attribution happens inside generation. A failed candidate does not replace the usable current version. Resume retries the same candidate; polling does not create one.

`create_task` requires `source="new_task"` or `source="external_artifact"`. `start_revision` takes the previous revision and a request key, without an attribution ID. `submit_execution` takes the exact frozen request SHA, actual outcome, artifacts and optional events. Stable keys and event IDs are required for reliable retries. The public model ID is supplied with `model`. Model calls reuse the existing Authorization API Key, with the gateway URL and auth type determined internally. There is no `model_api` connection object or second credential.

Without `key`, `start_recipe_candidate` uses `"recipe-" + sha256("recipe:<task_id>:<revision_id>:<max_completion_tokens or empty>")`, the same key the TypeScript SDK derives. A failed candidate raises `RSICandidateFailed` from the wait helpers with the service's stable `code`, `phase`, `retryable`, `upstream_status` and `upstream_code` (from `error_detail`; older servers give only `code`). Every returned JSON object exposes the response's `x-request-id` as `.request_id`.

Current HTTP and MCP schemas are supplied by the algorithm service. Old Run, attribution, acceptance, rubric, learning-job and administrative Recipe publication operations are absent.

### Recipe completion budget

`start_recipe_candidate(..., max_completion_tokens=200000)` and
`resume_recipe_candidate(..., max_completion_tokens=300000)` accept an optional integer
from 1 through 1,000,000. Omitting it leaves the initial default to the algorithm service
(200,000 in the updated service); a resumed candidate keeps its stored budget unless
explicitly overridden. The same budget applies to both feedback attribution and Recipe
creation/update. Model/gateway limits still apply; an accepted SDK value is not a promise
that every upstream model supports it.

The sync/async auto wrapper exposes the same option on
`client.cosens.generate_recipe(turn_id, max_completion_tokens=200000)` and
`client.cosens.resume_learning(turn_id, max_completion_tokens=300000)`. Task/Agent Runner
wrappers forward it from `generate_recipe` as well. It does not change business response
limits or `decision_max_output_tokens`. Reusing a generation key with a different explicit
budget is rejected; use resume to adjust a failed candidate, or a new generation key for
a new version. The Recipe service must be upgraded before using this field; older servers
reject unknown request fields. Published packages are not updated by a source checkout.

## Local RSI

`from cosens.local import LocalRSI, ChatCompletionsModel, CodexCLIModel`. Requires `cosens-sdk[local]`.

`LocalRSI(*, memory, model, state_path=None)` stores state in SQLite (default `~/.cosens/local-rsi.sqlite3`). `memory` is a `CoSensClient` used only for `/v1/memory/*`, or `None`.

Same names, parameters and responses as `client.atoms`: `create_task`, `get_task`, `list_tasks`, `supplement_task_context`, `artifact`, `get_artifact`, `start_execution`, `get_execution`, `submit_execution`, `get_revision`, `start_revision`, `record_feedback`, `start_recipe_candidate`, `get_recipe_candidate`, `resume_recipe_candidate`, `list_recipe_candidates`, `wait_workflow_job`, `get_task_recipe`, `get_recipe_versions`.

Differences from `client.atoms`:

- `model` and `max_completion_tokens` are set on the model object, not per call.
- `create_task(..., recipe=item)` starts a Task's Recipe chain from an item returned by `recall_recipes`.
- `recall_recipes(query, token_budget=8000)` returns Recipes published to Memory.
- `publish_recipe(recipe_id)` retries publishing the current version.
- `wait_workflow_job` runs the generation. A candidate nobody waits on is not generated.
- A `wait_workflow_job` timeout leaves the generation running in this process; wait on the same ID to continue.
- Candidates do not report `memory_usable`, `memory_validation` or `max_completion_tokens`, and candidate lists do not report `user_id`.
- Recipe candidates also report `publish_state`: `pending`, `published`, `failed` or `publish_conflict`.

`ChatCompletionsModel(client, model, *, max_completion_tokens=32000, timeout=300, extra_body=None)` takes your `AsyncOpenAI` client.

`CodexCLIModel(*, model=None, reasoning_effort="medium", timeout=300, executable="codex")` runs your installed Codex CLI and uses its logged-in quota. Each call is isolated from your Codex hooks, MCP servers and working directory.

Local RSI does not re-check whether Memory cited by a Recipe has since changed.
