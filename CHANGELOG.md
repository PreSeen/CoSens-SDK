# Changelog

Python (`cosens-sdk` on PyPI) and TypeScript (`cosens-sdk` on npm) are versioned separately.

## Unreleased

### Breaking

The SDKs follow the service's execution API: Task → Execution → Recipe. Revisions, Recipe candidates and Sessions are gone; feedback, corrections and learning name the execution. Python and TypeScript change alike unless a line says otherwise.

- RSI workflow routes are called without a `/v1` prefix (`/tasks`, `/executions/{id}`, `/recipes`, …). Memory and document routes keep `/v1`.
- A task is a capability, such as writing the weekly management report, that many executions reuse: `create_task(key=..., objective=..., artifacts=None)` / `createTask({ key, objective, artifacts })` takes no `source` or `self_contained`. A task's artifacts are bare ids or `{artifact_id, role: "reference"}`, and the task is `{task_id, objective, artifacts, context_version}`. `supplement_task_context` / `supplementTaskContext` takes only `objective` and reference `artifacts` besides `expected_context_version`. `get_task` / `getTask` and the items of `list_tasks` / `listTasks` return that task with its `executions` (each `{execution_id, instance_id, kind, status, request_sha256}`) and its `recipes`, each `{recipe_id, current_version, latest_version, learning}`; `source`, `mode`, `context`, `status`, `missing` and `recipe` are gone.
- Executions start at `POST /executions` (`POST /tasks/{id}/executions` is gone) with `task_id`, `source` and their own input: `start_execution(task_id, key=..., source=..., objective=None, artifacts=None, self_contained=None, recipe_id=None, model=None, recall_memory=False)` / `startExecution(taskId, { key, source, objective, artifacts, selfContained, recipeId, model, recallMemory })`. An input artifact given as a bare id is the `original_input` of a `new_task` and the `submitted_output` of an `external_artifact`. A new execution without `task_id` and `source`, or an `external_artifact` without a `submitted_output`, is rejected before sending. Requests freeze `task_context` (`objective`, `artifacts`, `context_version`) and `input` in place of `context` and `context_version`. Start receipts and `get_execution` / `getExecution` report `instance_id`, the first draft of the execution's correction chain.
- `needs_context` is gone: an incomplete input is an HTTP 422 `input_incomplete`, and `RSIHTTPError.missing` (both languages) names what it lacks: `objective`, and for `external_artifact` also `original_input_or_self_contained_task` or `submitted_output`. A task with several Recipes and a start that names none is a 422 `recipe_choice_required`, whose `RSIHTTPError.recipe_ids` / `recipeIds` lists the task's Recipes. Both fields are lists of names, otherwise `None` / `undefined`. The flows no longer wait for a task to be `ready` or supplement its context; the task runner raises the 422 unchanged, and the same turn may then be retried with a corrected task spec that keeps the objective (`Workflow task objective changed; start a new turn` otherwise). After any other failure of the start, or after a start, a changed spec is still `Workflow task changed while recovering the same turn`.
- A Recipe belongs to exactly one task: `recipe_id` / `recipeId` on a new execution must name a Recipe of that execution's task (HTTP 404 otherwise; the "task is already bound to another Recipe" error is gone). A learning adds its version to the Recipe the learned execution followed, and a correction follows the current version of that Recipe.
- Removed atoms, with no aliases: `get_revision` / `getRevision`, `start_revision` / `startRevision`, `start_recipe_candidate` / `startRecipeCandidate`, `get_recipe_candidate` / `getRecipeCandidate`, `resume_recipe_candidate` / `resumeRecipeCandidate`, `list_recipe_candidates` / `listRecipeCandidates`, `wait_workflow_job` / `waitWorkflowJob`, `recall_context` / `recallContext`, and the Session atoms `start_session`, `append_session_events`, `get_session_context`, `end_session` (TypeScript `startSession`, `appendSessionEvents`, `getSessionContext`, `endSession`). `recipe_request_key` / `recipeRequestKey` are removed.
- New atoms `learn(execution_id, model=None, max_completion_tokens=None)`, `resume_learning(...)` and `wait_learning(execution_id, timeout=300)` (TypeScript `learn`, `resumeLearning`, `waitLearning`). `learn` and `resume_learning` send no idempotency key: an execution learns once, a repeat returns the same learning, and a different budget is rejected with HTTP 422. `wait_learning` polls the execution (2 s doubling to 30 s) and returns a `completed` learning.
- `RSICandidateFailed` / `RSICandidateFailedError` are replaced by `RSILearningFailed` / `RSILearningFailedError`, raised by `wait_learning` for a `failed` learning, with `execution_id`, `code`, `message` (the service's text), `retryable`, `phase`, `upstream_status`, `upstream_code` and `request_id` (TypeScript: camelCase, and the service's text is `detailMessage` because `message` is the error's own). `retryable` is three-way: true when a resume may succeed, false when it will not, and `None` / `undefined` when the learning does not say; a bare code such as `previous_version_failed` has no detail and can still be resumed.
- New `RSILearningStopped` / `RSILearningStoppedError`, raised by `wait_learning` when a learning is `interrupted`, `awaiting_runner` or `awaiting_material`, the states only the caller can change. It carries `learning`, `execution_id`, `status` and `request_id`, and is not a failure. An execution without a learning, or a learning with an unknown status, raises `RSIRecoveryRequired`; a learning still in flight at the deadline raises a timeout error.
- `start_execution(key=..., previous_execution_id=...)` / `startExecution(undefined, { key, previousExecutionId })` starts a correction of a completed execution; a `task_id` / `taskId` is optional and must be the previous execution's task. It takes only `model` besides: `source`, an input field, `recipe_id` / `recipeId` or `recall_memory` / `recallMemory` with it is rejected before sending with `a correction takes only previous_execution_id, task_id and model` (in TypeScript it also fails to compile). The correction keeps the previous execution's `task_context`, `input` and `instance_id`.
- `record_feedback(execution_id, ...)` / `recordFeedback(executionId, ...)` records feedback on an execution.
- `submit_execution` / `submitExecution` returns `{task_id, execution_id, status, trace_origin}` and no `revision_id`; an execution is done when `status` is `completed`. Turns record `execution_id` and, once learned, `learning`; they no longer hold revision or candidate fields.
- `CoSensClient.start(..., phase=...)` takes `prepare`, `correct` or `learn` (was `prepare`, `revise`, `recipe`). `correct` starts a correction of `execution_id` and rejects `recipe_id` and `recall_memory`; only `prepare` and `correct` need a request key. The `prepare` and `correct` arguments are checked before the host is asked, so a refusal never follows an approval.
- `CoSensClient.start(..., phase='prepare')` needs the snapshot's `task`, this execution's `{source, objective, artifacts, self_contained}`; a snapshot with only `{task_id}` is no longer valid and raises `prepare requires task: the execution's source, objective and artifacts`. With a snapshot `task_id` the execution runs in that task; with neither it nor a `recipe_id`, the prepare creates a task named by `task.objective`. With `recipe_id` / `recipeId` it reads `get_recipe` and runs in the task that owns the Recipe, and the host approves the snapshot with that `task_id`; a Recipe without a task raises `Recipe X belongs to no task`, and a snapshot naming another task raises `Recipe X belongs to task A, not B`, before the host is asked.
- `CoSensClient.start(..., phase='prepare')` takes `model` only when it creates the task. With `recipe_id` / `recipeId` or a snapshot `task_id` the execution runs in an existing task, which keeps its frozen model, so a `model` there is rejected before the host is asked.
- `generate_recipe` / `generateRecipe` on the wrappers no longer take `request_key` / `requestKey`: learning is once per execution, and a changed budget is rejected.
- Python: `upload_recipe(recipe_id, version=..., recipe=..., previous_version=None)`: `base_version` is renamed `previous_version`, as the service requires.
- The auto Recipe lookup uses `list_recipes(limit=100, origin="generated")` and `get_recipe`, and offers the matcher only generated Recipes, because only those can be applied to an execution. The matcher answers with a `recipe_id`, and the execution's method is checked by `recipe_id` and `version`.
- Auto mode reuses the matched Recipe's task: a turn that matches a Recipe creates no task and starts its execution in the task that owns the Recipe (the `task_id` of the Recipe's list item and `get_recipe` view), with its `recipe_id` and without the turn's `model`, because the service freezes a task's model and every version of a Recipe stays on one model. A matched Recipe without a task is refused during the lookup (`Matched Recipe has no task`), so the turn passes through natively. A turn that matches no Recipe creates a task named by its objective, with the turn's `model`; each execution's input is the turn's request.
- Python: a follow-up message the decision classifies as feedback is recorded on the previous turn's execution, and the turn runs as a correction (key `auto-correction:<turn_id>`). A correction turn makes no Recipe lookup: it injects the current version of the Recipe the previous execution followed, which the service binds to the correction, and no Memory evidence. The transparent wrapper's `wait_learning(turn_id)` raises `RSILearningFailed` or `RSILearningStopped` after storing the learning record, so `resume_learning(turn_id)` can continue it.
- Sessions are removed: no Session atoms, and the Python agents adapter no longer syncs a server Session. The Session cursor keeps only what the wrappers use, Session snapshots (and, in TypeScript, `acquireSession`); its batch and ACK methods are gone: Python `CursorStore.pending`, `begin_batch`, `recover_batch`, `ack`, `last_ack` and `PendingBatch`; TypeScript `CursorStore.pending`, `beginBatch`, `recoverBatch` and `ack`.
- TypeScript has no automatic feedback-to-correction (use a `CoSensClient`'s `start(snapshot, { phase: 'correct' })`) and no `waitLearning` / `resumeLearning` on the wrappers (use a `CoSensClient`'s `atoms`, or the agent runner's `client.cosens.atoms`).
- LocalRSI keeps its earlier in-process names for corrections and learning (`get_revision`, `start_revision`, `start_recipe_candidate`, `wait_workflow_job`, …). Its `create_task`, `supplement_task_context`, `get_task`, `list_tasks` and new-execution `start_execution` take and return the same capability task and execution input shapes as `client.atoms`, and every execution reports `instance_id`. Its `RSICandidateFailed` now imports from `cosens.local.recipes`.
- LocalRSI's evidence references and learning history name executions: Recipe steps, lessons and attribution cite `{execution_id, event_id}`, history entries and their feedback carry `execution_id`, a revision execution appears as `correction`, and the context names `source_execution_id`. Opening a store written by an earlier SDK upgrades it once, in one transaction. If a stored Recipe or context does not match its hash, or names a revision the store lacks, opening raises `cosens.local.LocalStoreUpgradeError`, naming the store, the candidate and the column, and changes nothing; open the store with the previous release or move the file aside. A store written by a newer release is refused the same way. Stop older LocalRSI processes before upgrading; downgrading a store is not supported.
- LocalRSI's store moves to version 2 (`PRAGMA user_version`) on its first open, in the same transaction, as the service's migration 015 does: each task keeps only its objective, each request and start receipt freezes `input` and `task_context` with its request hash unchanged, every execution gets its `instance_id`, and each learning context becomes `workflow-recipe-context-v3` with its hash recomputed. A store in which one Recipe is bound to more than one task is not upgraded: opening raises `LocalStoreUpgradeError` naming the Recipes and their tasks, and nothing is changed; keep using it with the previous release, or move the file aside. Old `create_task` and `start_execution` idempotency keys no longer match any request, so start executions under new keys and keep using an existing task by its `task_id`.

### Migrating

| Earlier call or check | Now (Python / TypeScript) |
| --- | --- |
| `get_revision(revision_id)` / `getRevision` | `get_execution(execution_id)` / `getExecution`; its `output` is what the revision held |
| `start_revision(revision_id, key=...)` / `startRevision` | `start_execution(key=..., previous_execution_id=execution_id)` / `startExecution(undefined, { key, previousExecutionId })` |
| `start_recipe_candidate(task_id, revision_id=...)` / `startRecipeCandidate` | `learn(execution_id)` / `learn(executionId)` |
| `get_recipe_candidate(id)` / `getRecipeCandidate` | `get_execution(execution_id)["learning"]` / `(await getExecution(executionId)).learning` |
| `resume_recipe_candidate(id)` / `resumeRecipeCandidate` | `resume_learning(execution_id)` / `resumeLearning(executionId)` |
| `wait_workflow_job("recipe_candidate", id)` / `waitWorkflowJob` | `wait_learning(execution_id)` / `waitLearning(executionId)` |
| `list_recipe_candidates(task_id=...)` / `listRecipeCandidates` | the `learning` of the task's executions (`get_task(task_id)["executions"]`, then `get_execution`), or `get_recipe_versions(recipe_id)` / `getRecipeVersions` |
| `recall_context(query=...)` / `recallContext` | `memory_recall(query=...)` / `memoryRecall` for the Memory part; there is no Recipe-lookup equivalent |
| `start_session`, `append_session_events`, `get_session_context`, `end_session` (and TypeScript equivalents) | none |
| `RSICandidateFailed` / `RSICandidateFailedError` | `RSILearningFailed` / `RSILearningFailedError`, plus `RSILearningStopped` / `RSILearningStoppedError` |
| a turn's `revision_id` (`turn.get("revision_id")` / `turn.revisionId`) | `turn["state"] == "completed"` and `turn["execution_id"]` / `turn.state === 'completed' && turn.executionId` |
| `submit_execution(...)["revision_id"]` | `submit_execution(...)["status"] == "completed"` |
| `start(..., phase="revise" / "recipe")` | `phase="correct"` / `phase="learn"` |
| `generate_recipe(turn_id, request_key=...)` / `generateRecipe({ requestKey })` | `generate_recipe(turn_id)` / `generateRecipe({ turnId })` |
| `create_task(source=..., objective=..., artifacts=..., self_contained=..., key=...)`, then `start_execution(task_id, key=...)` | `create_task(key=..., objective=...)`, then `start_execution(task_id, key=..., source=..., objective=..., artifacts=..., self_contained=...)` / `startExecution(taskId, { key, source, objective, artifacts, selfContained })` |
| a task's `status == "needs_context"` and its `missing` | a 422 `input_incomplete` from the start, `RSIHTTPError.missing` |
| a task's `recipe` | a task's `recipes` |
| a request's `context` and `context_version` | its `input` and `task_context` (`task_context["context_version"]`) |
| `start({"task_id": task_id}, phase="prepare", ...)` | `start({"task_id": task_id, "task": {"source": ..., "objective": ..., "artifacts": ..., "self_contained": ...}}, phase="prepare", ...)` |

### Added

- TypeScript gains the Recipe store atoms `uploadRecipe(recipeId, { version, recipe, previousVersion })`, `getRecipe`, `listRecipes` and `searchRecipes`.

### Changed

- `cosens.local` freezes the Memory recall as the service returns it: the original text of the episodes the general Memory recall finds, at most 8 sources within the token budget. The service no longer returns source references, so the snapshot drops the score, duplicate and reference filter (`omitted_unreferenced`, `omitted_low_relevance` and `omitted_duplicate` are gone; `omitted_over_budget` counts sources beyond the limit).

## Python 0.7.0

### Breaking

- `cosens.local` uploads each new current Recipe version to the service's Recipe store (`/v1/recipes`) instead of publishing it to Memory. The upload carries no evidence references and names the last version the store is known to have, so a version uploaded first from another machine makes it a `publish_conflict`; the local version stands either way. `publish_recipe(recipe_id)` retries the upload.
- `LocalRSI.recall_recipes` is removed. Use `search_recipes(query, limit=10)`, `list_recipes(limit=20, cursor=None, origin=None)` and `get_recipe(recipe_id, version=None)`. `create_task(..., recipe=...)` takes a Recipe returned by `get_recipe`: an uploaded Recipe continues under its own `recipe_id` and version, and a Recipe generated by the service starts a new local chain without its evidence references.
- `LocalRSI(memory=...)` is renamed `LocalRSI(client=...)`, because the client now serves Memory recall and the Recipe store. `memory=` still works and emits a `DeprecationWarning`.
- Recipes that 0.6 published to Memory are not read. A 0.6 local database is upgraded in place when opened, and its Recipe versions become `pending` uploads, because none of them is in the Recipe store yet.

### Added

- `upload_recipe`, `get_recipe`, `list_recipes` and `search_recipes` on `client.atoms` and `client.sync_atoms`.
- `RSIHTTPError.current_version` carries the chain's current version from a Recipe version conflict.
- `contract/local-rsi/recipe-upload.schema.json` pins the uploaded Recipe's schema.

### Changed

- `cosens.local` writes Recipes that carry more of what a task teaches. Generation keeps every convention that carries across tasks: value tables, thresholds, formulas, allowed values, status rules, required output fields, and name formats as placeholder patterns. Where a reference gives numeric results beside the labels that produced them, it fits and checks the simplest rule that reproduces every row. Each case-to-action mapping lists the field values that set the case apart, and says what the references do when no mapping matches. See [the research notes](python/docs/research/local-recipe-generation.md).
- A new Recipe version keeps the steps and lessons of the previous one for other kinds of task, with their evidence references, and may cite evidence the previous version already cited.
- A Recipe may hold up to 30 steps, 20 lessons and 32,000 characters of text (was 8 steps, 5 lessons and 6,000 characters). Step instructions may be 1,200 characters, `when` and `when_not` 1,500.
- Memory recall for a Recipe uses a token budget of 8,000 (was 2,000), enough for several reference sources of 2-3k tokens each.

## Python 0.6.1

### Fixed

- `import cosens.local` without the `local` extra raises an `ImportError` that names `pip install 'cosens-sdk[local]'` instead of a bare `ModuleNotFoundError: jsonschema`.

## Python 0.6.0

### Added

- `cosens.local.LocalRSI` runs the Task, Revision and Recipe workflow in your own process with your own model, through `ChatCompletionsModel` (your `AsyncOpenAI` client) or `CodexCLIModel` (your Codex login). CoSens is used only as Memory: the current Recipe version is published there and `recall_recipes` finds it from another machine. Install `cosens-sdk[local]`. TypeScript does not have this yet.
- `RSITransportError` is importable from `cosens`, like the other error classes (it was only in `cosens.errors`).

## Python 0.5.0 / TypeScript 0.6.0

### Breaking

- `include_memory` / `includeMemory` on `start_execution` / `startExecution` (and on `start` / `startWorkflow`) is renamed `recall_memory` / `recallMemory`. It asks the RSI service to recall Memory by the task objective and freeze it into the execution request; a Recipe generated from that execution recalls Memory only when it was set. The service rejects the old name.
- `record_feedback` / `recordFeedback` require `source`, one of `human`, `evaluator`, `reference`, `automated_check` or `downstream`, so Recipe attribution can weigh feedback by where it comes from. The runner and proxy `feedback(...)` helpers take it too; the OpenAI proxy records a user's follow-up message as `human`.
- The provider-specific model client factory and its base URL constant were removed from both SDKs. Build a standard OpenAI client for your model gateway and pass it to `cosens(...)`.
- The `auto` decision defaults changed: 5-second timeout (was 60), a 1024-token output budget including reasoning (was uncapped in Python, 256 in TypeScript), and no retries.
- The decision model now writes only `activate` and `reason` (plus `feedback` for a linked turn). The task objective is the user's text and `task_family` is `"general"`. Successful decisions on `deepseek-flash` used at most 139 output tokens (294 with feedback) instead of up to 826.
- A Recipe-match call that times out, is cut off or returns an invalid result no longer discards the capture: the turn is recorded without a Recipe and carries `recipe_match_error`.
- The wait helpers (`wait_workflow_job`, `wait_learning`, `waitWorkflowJob`) raise `RSICandidateFailed` / `RSICandidateFailedError` when a Recipe candidate failed, instead of returning it.
- Injected Recipe/Memory context is plain text bounded by `max_context_chars` / `maxContextChars` (default 4000), not the raw JSON bundle. Evidence references and Memory scores are no longer sent to the model. Nothing is injected when there is no matched Recipe and no Memory.

### Added

- `decision_client` / `decisionClient` and `decision_extra_body` / `decisionExtraBody` for the decision and Recipe-match calls, e.g. a separate non-reasoning model, or a reasoning switch your gateway accepts.
- `max_context_chars` / `maxContextChars` on `cosens(...)` and the task workflow runners.
- `RSICandidateFailed` (Python) / `RSICandidateFailedError` (TypeScript) with `code`, `phase`, `retryable`, `upstream_status`, `upstream_code`, `candidate_id` and `request_id`, read from the service's `error_detail`.
- Successful responses expose `x-request-id` as `.request_id` (Python) / non-enumerable `requestId` (TypeScript).
- `key` is optional on `start_recipe_candidate` / `startRecipeCandidate`; the default is derived from the task, revision and completion budget, identically in both SDKs.

### Fixed

- A request without user text makes no decision call and is recorded as `capture: "no_user_text"`.

- Python: a failed or timed-out decision now records a turn (`last_turn_id` is set) with `capture: "decision_failed"` and `decision.error_type` / `decision.error`, as TypeScript already did. Output cut off at the token limit (`finish_reason=length`) is reported as such. `get_auto()` includes `capture`, `decision_reason` and `decision_error_type`.
- Python: `client.cosens.get_turn(None)` returns `None` instead of raising `TypeError`.

- TypeScript: a declined (`activate: false` or `mode: 'disable'`) turn no longer lists Recipes, runs the Recipe-match model call or injects context (Python fixed this in 1baf0ef).
- An empty Recipe list is cached for 60 seconds and skips the Recipe-match call; candidate details are fetched concurrently; the matcher receives only title, when and when_not.

### Packaging

- Both packages are licensed under Apache-2.0. `LICENSE` and `NOTICE` ship in the wheel, sdist and npm package, and the package metadata carries the SPDX identifier.
