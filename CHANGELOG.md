# Changelog

Python (`cosens-sdk` on PyPI) and TypeScript (`cosens-sdk` on npm) are versioned separately.

## Unreleased

### Fixed

- Python: `import cosens.local` without the `local` extra raises an `ImportError` that names `pip install 'cosens-sdk[local]'` instead of a bare `ModuleNotFoundError: jsonschema`.

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
