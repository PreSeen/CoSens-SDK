# Changelog

Python (`cosens-sdk` on PyPI) and TypeScript (`cosens-sdk` on npm) are versioned separately.

## Python 0.9.2

- Cloud `recipe_insert(path)` uploads your own Recipe: the SDK validates the Skill Markdown file locally (YAML frontmatter with nonempty `name` and `description`) and sends it to `POST /recipes`, which publishes it as version 1. Local mode is unchanged.

## Python 0.9.1

- Supported Memory calls can specify an HTTPS service URL and explicit API key per call.
- Python and TypeScript cloud RSI clients support explicit Virtual User selection. Supported Memory calls accept a per-call selection; unsupported Memory overview and experience calls reject it.
- TypeScript: cloud `RSIClient` options and Memory calls accept `virtual_user_id` as an alias for `virtualUserId`, so a snake_case selection is no longer silently ignored. Different values for the two throw a `TypeError`.
- Local clarification continuations can start automatically with refreshed headers from the same credential scope and model endpoint.
- Local `recover` accepts a Recipe ID and `retry_key` to resume a timed-out execution with its existing checkpoints and idempotent dispatch.
- Local Recipe tests can run Python in an isolated Docker workspace when a trusted local image is configured. Publication requires actual execution evidence when the task calls for Python.

## Python 0.9.0

- `RSIClient()` with no arguments is **cloud mode**: the CoSens service learns with CoSens models, authenticated by `COSENS_API_KEY` (or `api_key=`, `base_url=`). Only the files named in `evidence` are uploaded. Methods and results are the same as local mode; `recover()` returns `{"started": 0}`.
- `RSIClient(data_dir=..., config=...)` (or `mode="local"`) keeps the local Worker unchanged.
- TypeScript: `new RSIClient()` is cloud mode, `new RSIClient({ data_dir, config })` local. All `RSIClient` methods are now async in both modes.

## Python 0.8.3

- A final structured result whose fields arrive as the text `"None"` or `"null"` is read as null, and objects sent as JSON strings are decoded leniently, so models that stringify nested results (seen with qwen3.8-27b) no longer fail analysis after three attempts.
- The retry message lists what is still wrong after that decoding, not the original type errors.

## Python 0.8.2

- `config` accepts optional `max_tokens` and `timeout` (seconds). `max_tokens` is no longer fixed at 4096: when unset, requests leave the completion budget to the model endpoint, so reasoning models are not cut off.
- Model requests name the SDK in the User-Agent (`cosens-sdk-python/<version>`, TypeScript `cosens-sdk-typescript/<version>`), which endpoints behind bot filters accept.
- When the model's final structured result does not validate, the retry message names the fields to fix, and objects or arrays sent as JSON strings are decoded before validation.

## Python 0.8.1

### Breaking

- `execution_create(evidence=..., idempotency_key=...)` no longer takes `base_recipe_id`. To revise a Recipe, add one evidence file with `role: "recipe"`; with `recipe_id` the new version joins that Recipe (which must exist and have an available version), without it a new Recipe is created. The returned execution carries `recipe_id` from the moment it is queued.
- `execution_list(query=None)` replaces paging: it returns the unarchived executions, or with `query={"execution_ids": [...]}` the matching ones (archived included), oldest first.
- `recipe_list(query=None)` accepts `query={"recipe_ids": [...]}`. Each item has `execution_ids` (most active first) in place of `execution_id`.
- `recipe_get` returns `id`, `name`, `created_at`, `updated_at`, `archived_at`, the three version IDs and `selected_version`; `revision` and `description` are gone.
- The local SQLite store upgrades to schema v5 in one transaction on start. Stop Workers of older versions that use the same data directory first.

## Python 0.8.0

### Breaking

The Python package is now the local RSI SDK, `cosens.rsi`. Learning runs on the developer's machine with the developer's own tool-calling model; the package no longer calls the CoSens service.

- New: `from cosens.rsi import RSIClient`, with `RSIClient(data_dir=..., config={"model", "base_url", "auto_start"})` and the atomic functions `execution_create`, `execution_get`, `execution_list`, `execution_append_evidence`, `execution_cancel`, `execution_clarification_answer`, `rubrics_list`, `rubrics_get`, `recipe_list`, `recipe_get`, `recipe_test`, `recipe_version_set_archived` and `recover`. See `docs/local-rsi.md`.
- A Worker queued in local SQLite extracts user feedback, analyzes the evidence, writes Rubrics and a Recipe, replays the task with the Recipe, scores the result independently and publishes the version that passes. The model key is read from `RSI_MODEL_API_KEY`.
- Removed with no aliases: the `cosens(...)` OpenAI wrapper, `CoSensClient`, the service atoms (tasks, executions, feedback, learning, Recipes, documents, Memory), the OpenAI Agents and Responses adapters, and `cosens.local.LocalRSI`. Applications that need them stay on 0.7.0.
- The hosted execution-API changes that were listed as unreleased after 0.7.0 were never published and are not part of this release.
- New dependencies: `langgraph` and `langgraph-checkpoint-sqlite`. The `agents` extra is gone.

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
