# Changelog

Python (`cosens-sdk` on PyPI) and TypeScript (`cosens-sdk` on npm) are versioned separately.

## Unreleased

### Breaking

The SDKs follow the service's execution API: Task → Execution → Recipe. Revisions, Recipe candidates and Sessions are gone; feedback, corrections and learning name the execution. Python and TypeScript change alike unless a line says otherwise.

- RSI workflow routes are called without a `/v1` prefix (`/tasks`, `/executions/{id}`, `/recipes`, …). Memory and document routes keep `/v1`.
- Removed atoms, with no aliases: `get_revision` / `getRevision`, `start_revision` / `startRevision`, `start_recipe_candidate` / `startRecipeCandidate`, `get_recipe_candidate` / `getRecipeCandidate`, `resume_recipe_candidate` / `resumeRecipeCandidate`, `list_recipe_candidates` / `listRecipeCandidates`, `wait_workflow_job` / `waitWorkflowJob`, `recall_context` / `recallContext`, and the Session atoms `start_session`, `append_session_events`, `get_session_context`, `end_session` (TypeScript `startSession`, `appendSessionEvents`, `getSessionContext`, `endSession`). `recipe_request_key` / `recipeRequestKey` are removed.
- New atoms `learn(execution_id, model=None, max_completion_tokens=None)`, `resume_learning(...)` and `wait_learning(execution_id, timeout=300)` (TypeScript `learn`, `resumeLearning`, `waitLearning`). `learn` and `resume_learning` send no idempotency key: an execution learns once, a repeat returns the same learning, and a different budget is rejected with HTTP 422. `wait_learning` polls the execution (2 s doubling to 30 s) and returns a `completed` learning.
- `RSICandidateFailed` / `RSICandidateFailedError` are replaced by `RSILearningFailed` / `RSILearningFailedError`, raised by `wait_learning` for a `failed` learning, with `execution_id`, `code`, `retryable`, `phase`, `upstream_status`, `upstream_code` and `request_id` (TypeScript: camelCase, and the service's text is `detailMessage` because `message` is the error's own). `retryable` is three-way: true when a resume may succeed, false when it will not, and `None` / `undefined` when the learning does not say; a bare code such as `previous_version_failed` has no detail and can still be resumed.
- New `RSILearningStopped` / `RSILearningStoppedError`, raised by `wait_learning` when a learning is `interrupted`, `awaiting_runner` or `awaiting_material`, the states only the caller can change. It carries `learning`, `execution_id`, `status` and `request_id`, and is not a failure. An execution without a learning, or a learning with an unknown status, raises `RSIRecoveryRequired`; a learning still in flight at the deadline raises a timeout error.
- `start_execution(task_id, key=..., previous_execution_id=...)` / `startExecution(taskId, { key, previousExecutionId })` starts a correction of a completed execution. It takes only `model` besides: `recipe_id` / `recipeId` or `recall_memory` / `recallMemory` with it is rejected before sending (in TypeScript it also fails to compile).
- `record_feedback(execution_id, ...)` / `recordFeedback(executionId, ...)` records feedback on an execution.
- `submit_execution` / `submitExecution` returns `{task_id, execution_id, status, trace_origin}` and no `revision_id`; an execution is done when `status` is `completed`. Turns record `execution_id` and, once learned, `learning`; they no longer hold revision or candidate fields.
- `CoSensClient.start(..., phase=...)` takes `prepare`, `correct` or `learn` (was `prepare`, `revise`, `recipe`). `correct` starts a correction of `execution_id` and forwards `recipe_id` and `recall_memory`, so the atom rejects them; only `prepare` and `correct` need a request key.
- `generate_recipe` / `generateRecipe` on the wrappers no longer take `request_key` / `requestKey`: learning is once per execution, and a changed budget is rejected.
- `upload_recipe(recipe_id, version=..., recipe=..., previous_version=None)` / `uploadRecipe(recipeId, { version, recipe, previousVersion })`: `base_version` is renamed `previous_version`, as the service requires.
- The auto Recipe lookup uses `list_recipes(limit=100, origin="generated")` and `get_recipe`, and offers the matcher only generated Recipes, because only those can be applied to an execution. The matcher answers with a `recipe_id`, and the execution's method is checked by `recipe_id` and `version`.
- Python: a follow-up message the decision classifies as feedback is recorded on the previous turn's execution, and the turn runs as a correction (key `auto-correction:<turn_id>`). A correction turn makes no Recipe lookup: it injects the Recipe the service binds to the correction from the task, and no Memory evidence. The transparent wrapper's `wait_learning(turn_id)` raises `RSILearningFailed` or `RSILearningStopped` after storing the learning record, so `resume_learning(turn_id)` can continue it.
- Sessions are removed: no Session atoms, and the Python agents adapter no longer syncs a server Session. The Session cursor keeps only Session snapshots; its batch and ACK methods are gone (TypeScript `CursorStore.pending`, `beginBatch`, `recoverBatch` and `ack`).
- TypeScript has no automatic feedback-to-correction (use `start({ phase: 'correct' })`) and no `waitLearning` / `resumeLearning` on the wrappers (use `client.atoms`).
- LocalRSI keeps its earlier in-process workflow names (`get_revision`, `start_revision`, `start_recipe_candidate`, `wait_workflow_job`, …) and does not yet follow the execution API. Its `RSICandidateFailed` now imports from `cosens.local.recipes`.
- LocalRSI's evidence references and learning history name executions: Recipe steps, lessons and attribution cite `{execution_id, event_id}`, history entries and their feedback carry `execution_id`, a revision execution appears as `correction`, and the context names `source_execution_id`. Opening a store written by an earlier SDK upgrades it once, in one transaction. If a stored Recipe or context does not match its hash, or names a revision the store lacks, opening raises `cosens.local.LocalStoreUpgradeError`, naming the store, the candidate and the column, and changes nothing; open the store with the previous release or move the file aside. A store written by a newer release is refused the same way. Stop older LocalRSI processes before upgrading; downgrading a store is not supported.

### Added

- TypeScript gains the Recipe store atoms `uploadRecipe`, `getRecipe`, `listRecipes` and `searchRecipes`.

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
