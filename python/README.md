# CoSens Python SDK

Install with `pip install .` from this directory. Agent support: `pip install '.[agents]'`.

```python
from cosens import cosens, friday_gateway_client

client = cosens(friday_gateway_client(), mode="auto")
result = client.responses.create(model="<public-model-id>", input="Produce a report")
turn_id = client.cosens.last_turn_id
```

Set `COSENS_API_KEY` and optionally `COSENS_REGION=cn|us` (default `cn`). Model and CoSens clients reuse the same Friday user API Key. The application loads `.env`; the SDK reads process environment. Use a model ID from the regional gateway catalog.

The response is the native OpenAI result. After your application obtains user confirmation of the artifact, call from its confirmation handler:

```python
receipt = client.cosens.generate_recipe(turn_id, request_key="confirmed-report-1")
```

This is the only generation action. `feedback` records feedback; it does not update a Recipe. Existing Recipes are updated through the same explicit generation API. Generation reads relevant Memory by default and performs attribution internally when feedback is present.

[Agent execution](examples/02_explicit_run.py) and [atomic calls](examples/03_atomic.py) follow the same rule. Async OpenAI clients expose async confirmation calls; atomic operations are available through `CoSensClient.atoms` (async) and `.sync_atoms` (sync). Keep stable idempotency keys across retries.

[API reference](docs/api-reference.md). Run tests with `PYTHONPATH=src pytest tests`.
