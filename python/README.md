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

Only the files named in `evidence` are uploaded. For local mode, pass a data directory and your own tool-calling model; the key comes from `RSI_MODEL_API_KEY`:

```python
rsi = RSIClient(
    data_dir="./.rsi-python",
    config={"model": "your-model", "base_url": "http://127.0.0.1:8000/v1", "auto_start": True},
)
```

Both modes have the same methods and results. See the [atomic API and Hook lifecycle](docs/local-rsi.md) and the [Python Hook example](examples/05_rsi_sdk_v2.py).
