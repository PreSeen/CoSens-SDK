# CoSens Python RSI SDK

Install with `pip install --upgrade cosens-sdk`.

Cloud mode is the default. Set `COSENS_API_KEY`; CoSens learns with its own models and you need no model of your own.

```python
from cosens.rsi import RSIClient

rsi = RSIClient()  # cloud
run = rsi.execution_create(
    evidence={"context_path": "context.txt", "files": [{"path": "sales.csv", "role": "input"}]},
    idempotency_key="delivery-42",
)
print(rsi.execution_get(run["id"])["status"])
```

The cloud SDK uploads files listed in `evidence` and the optional `recipe_reference`. For local mode, pass a data directory, the tool-calling model name and the host's model request `headers`.

```python
rsi = RSIClient(
    data_dir="./.rsi-python",
    config={"model": "your-model", "base_url": "http://127.0.0.1:8000/v1", "headers": {}, "auto_start": True},
)
```

After refreshing a token, create a Client with the same data directory and `credential_scope` and the new headers. `rsi.recover()` resumes that account's unfinished work with the new Client settings, and `rsi.recover(recipe_id=recipe_id)` limits recovery to one SDK Recipe. Local Recipe reads include a `storage_path` relative to `data_dir`. See the [atomic API and Hook lifecycle](docs/local-rsi.md) and the [Python Hook example](examples/05_rsi_sdk_v2.py).

### Virtual User selection

For cloud RSI, use `RSIClient(virtual_user_id="vu_...")` to bind this client to a Virtual User. The option sets the `X-Virtual-User-Id` request header, which is how the service identifies the Virtual User. Use separate clients for separate Virtual Users. Local RSI does not accept this option. Memory's `memory_recall`, `memory_ingest`, `get_memory_items`, and `get_memory_job` take an optional `virtual_user_id` per call. `get_memory` and `get_memory_experience` do not support Virtual User selection and reject it. Calls without a selection use the authenticated account. The server checks each caller's current grant; availability depends on the service deployment.

For a different Memory deployment, pass both `base_url` (the HTTPS Memory API URL, including its deployment-specific path) and `api_key` to `memory_recall`, `memory_ingest`, `get_memory_items`, or `get_memory_job`. For example, `await memory_recall(query="...", base_url="https://memory.example/memory", api_key="...")`. The explicit key is used only for that call; the configured URL cannot contain credentials, a query, a fragment, or path traversal, and redirects are not followed.

To explicitly retry a timed-out local execution, call `rsi.recover(recipe_id=recipe_id, retry_key=request_id)`. Reuse the key if the request is submitted again.
