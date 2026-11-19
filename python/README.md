# CoSens Python Local RSI SDK

Install with `pip install cosens-sdk` when the package version containing this SDK is available, or `pip install ./python` from a checkout.

```python
from cosens.rsi import RSIClient

rsi = RSIClient(
    data_dir="./.rsi-python",
    config={"model": {"model": "your-model", "base_url": "http://127.0.0.1:8000/v1"}},
)
```

The host supplies a tool-calling model and sets `RSI_MODEL_API_KEY` in its environment. See the [atomic API and Hook lifecycle](docs/local-rsi.md) and the [Python Hook example](examples/05_rsi_sdk_v2.py).
