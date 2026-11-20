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

Both modes expose the same methods. Local Recipe reads include a `storage_path` relative to `data_dir`. See the [atomic API and Hook lifecycle](docs/local-rsi.md) and the [Python Hook example](examples/05_rsi_sdk_v2.py).
