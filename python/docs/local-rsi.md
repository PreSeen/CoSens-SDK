# Local RSI

`from cosens.local import LocalRSI, ChatCompletionsModel, CodexCLIModel`. Requires `cosens-sdk[local]`.

`LocalRSI(*, client=None, model, state_path=None)` stores state in SQLite (default `~/.cosens/local-rsi.sqlite3`). `client` is a `CoSensClient` used only for Memory recall (`/v1/memory/*`) and the Recipe store (`/recipes`), or `None`. `memory=` is a deprecated name for `client=`. Call `await rsi.aclose()` when done; it returns any running generation to the queue.

`LocalRSI` keeps its earlier in-process workflow names (`get_revision`, `start_revision`, `start_recipe_candidate`, `wait_workflow_job`, …); it does not yet follow the execution API. Its methods are `create_task`, `get_task`, `list_tasks`, `supplement_task_context`, `artifact`, `get_artifact`, `start_execution`, `get_execution`, `submit_execution`, `get_revision`, `start_revision`, `record_feedback(revision_id, ...)`, `start_recipe_candidate`, `get_recipe_candidate`, `resume_recipe_candidate`, `list_recipe_candidates`, `wait_workflow_job`, `get_recipe_versions` and `get_task_recipe`. A failed candidate raises `RSICandidateFailed`, imported from `cosens.local.recipes`.

Its evidence references now use `execution_id`: Recipe steps, lessons and attribution cite `{execution_id, event_id}`, and the learning history names each entry's execution (and its feedback's), calls a revision execution a `correction`, and names its source execution. This matches the service and the shared `contract/local-rsi` fixture. A store written by an earlier SDK is upgraded once, in one transaction, when it is first opened; if a stored Recipe or context no longer matches its hash, or names a revision the store lacks, opening raises and the store is left unchanged.

Other differences from `client.atoms`:

- `model` and `max_completion_tokens` are set on the model object, not per call.
- Each new current version is uploaded to the Recipe store with `upload_recipe`, without evidence references, under the chain's `recipe_id`. `previous_version` is the last version the store is known to have, so a version uploaded first from another machine makes the upload a `publish_conflict`; when the store is ahead with this chain's own earlier version, whose response was lost, the upload is retried on top of it. The local version stands either way.
- `search_recipes(query, limit=10)`, `list_recipes(limit=20, cursor=None, origin=None)` and `get_recipe(recipe_id, version=None)` read the Recipe store, as on `client.atoms`.
- `create_task(..., recipe=view)` takes a Recipe returned by `get_recipe`. An `uploaded` Recipe continues under its own `recipe_id`: the next local version is its version plus one and is uploaded on top of it; if the chain is already on this machine, pass its `recipe_id` to `start_execution` instead. A `generated` Recipe starts a new local chain at version 1 without its evidence references. Anything that does not match `contract/local-rsi/recipe-upload.schema.json` is rejected.
- `publish_recipe(recipe_id)` retries uploading the current version.
- `wait_workflow_job` runs the generation. A candidate nobody waits on is not generated.
- A `wait_workflow_job` timeout leaves the generation running in this process; wait on the same ID to continue.
- Candidates do not report `memory_usable`, `memory_validation` or `max_completion_tokens`, and candidate lists do not report `user_id`.
- Recipe candidates also report `publish_state`: `pending`, `published`, `failed` or `publish_conflict` (`None` without a client, and for a version started from a generated Recipe).

`ChatCompletionsModel(client, model, *, max_completion_tokens=32000, timeout=300, extra_body=None)` takes your `AsyncOpenAI` client.

`CodexCLIModel(*, model=None, reasoning_effort="medium", timeout=300, executable="codex")` runs your installed Codex CLI and uses its logged-in quota (Linux and macOS). Each call is isolated from your Codex hooks, MCP servers and working directory.

Local RSI does not re-check whether Memory cited by a Recipe has since changed.
