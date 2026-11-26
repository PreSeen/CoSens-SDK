# CoSens Python RSI SDK

Install with `pip install --upgrade cosens-sdk`.

Cloud mode is the default and uses `COSENS_API_KEY` for access to CoSens models. The default RSI API is `https://friday-rsi-cn.stardust-ai.com/api/v1`. Use `base_url` or `COSENS_RSI_BASE_URL` to specify a different API prefix. The SDK appends `/v1` when needed.

```python
from cosens.rsi import RSIClient

rsi = RSIClient()  # cloud
run = rsi.execution_create(
    evidence={"context_path": "context.txt", "files": [{"path": "sales.csv", "role": "input"}]},
    idempotency_key="delivery-42",
)
print(rsi.execution_get(run["id"])["status"])
```

The cloud SDK uploads files listed in `evidence` and the optional `recipe_reference`. To run local RSI, pass a data directory and the model's `model_id`, `api_key`, and `base_url`.

```python
import os

rsi = RSIClient(
    data_dir="./.rsi-python",
    model_id="your-model",
    api_key=os.environ["RSI_MODEL_API_KEY"],
    base_url="http://127.0.0.1:8000/v1",
)
```

The model must support OpenAI-compatible Chat Completions and tool calls. Use `config.headers` to authorize cloud Recipe downloads and uploads from a local client. Local `execution_create()` can download a missing published Recipe before queuing an update, and `recipe_reference.recipe_version_id` can pin the baseline. After rotating the model key, create a Client with the same data directory and `config.credential_scope` and the new `api_key`. `rsi.recover()` resumes that account's unfinished work with the new Client settings, and `rsi.recover(recipe_id=recipe_id)` limits recovery to one SDK Recipe. Local Recipe reads include a `storage_path` relative to `data_dir`. See the [atomic API and Hook lifecycle](docs/local-rsi.md) and the [Python Hook example](examples/05_rsi_sdk_v2.py).

### Virtual User selection

For cloud RSI, use `RSIClient(virtual_user_id="vu_...")` to bind this client to a Virtual User. The option sets the `X-Virtual-User-Id` request header, which is how the service identifies the Virtual User. Use separate clients for separate Virtual Users. Local RSI does not accept this option. Memory's `memory_recall`, `memory_ingest`, `get_memory_items`, and `get_memory_job` take an optional `virtual_user_id` per call. `get_memory` and `get_memory_experience` do not support Virtual User selection and reject it. Calls without a selection use the authenticated account. The server checks each caller's current grant; availability depends on the service deployment.

For a different Memory deployment, pass both `base_url` (the HTTPS Memory API URL, including its deployment-specific path) and `api_key` to any Memory function: `memory_recall`, `memory_ingest`, `get_memory_items`, `get_memory_job`, `get_memory_experience`, or `get_memory`. For example, `await memory_recall(query="...", base_url="https://memory.example/memory", api_key="...")`. The explicit key is used only for that call; the configured URL cannot contain credentials, a query, a fragment, or path traversal, and redirects are not followed.

Use `rsi.task_list()` for local task cards and `rsi.task_get(task_id)` for clarification history and the final ZIP. To start a fresh Execution in the same task, call `rsi.task_restart(task_id, idempotency_key=request_id)`. Reuse the key when resending the same restart request.

`rsi.task_get(task_id)` returns `recipe_version_id` and `recipe_zip_path` once the final ZIP is ready. Publish the task with `rsi.recipes_upsert(task_id=task_id)`. Pass `recipe_id=existing_id` to target an existing cloud Recipe. The upload uses the client's `config.headers` and the destination set by `COSENS_RSI_BASE_URL`, which defaults to the CoSens endpoint. A cloud client takes a ZIP path with `rsi.recipes_upsert(path)`.
