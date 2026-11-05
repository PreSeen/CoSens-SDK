# CoSens Python SDK

Install with `pip install .` from this directory. Agent support: `pip install '.[agents]'`.

```python
from cosens import cosens, friday_gateway_client

client = cosens(friday_gateway_client(), mode="auto")
result = client.responses.create(model="<public-model-id>", input="Produce a report")
turn_id = client.cosens.last_turn_id
```

Set `COSENS_API_KEY` and optionally `COSENS_REGION=cn|us` (default `cn`). Model and CoSens clients reuse the same Friday user API Key. The application loads `.env`; the SDK reads process environment. Use a model ID from the regional gateway catalog.

`auto` uses a 60-second timeout for its structured decision call by default. Pass `decision_timeout=<seconds>` to tune it for your gateway. If the decision or read-only Recipe lookup fails, the native model request still runs without CoSens capture.

The response is the native OpenAI result. In `auto` mode, a short request can be skipped (`activate: false`), leaving no Task or Revision. Check that the turn has a submitted Revision before offering Recipe generation. After your application obtains user confirmation of that artifact, call from its confirmation handler:

```python
turn = client.cosens.get_turn(turn_id) if turn_id else None
if turn and turn.get("revision_id"):
    receipt = client.cosens.generate_recipe(turn_id, request_key="confirmed-report-1")
```

This is the only generation action. `feedback` records feedback; it does not update a Recipe. Existing Recipes are updated through the same explicit generation API. Generation reads relevant Memory by default and performs attribution internally when feedback is present.

[Agent execution](examples/02_explicit_run.py) and [atomic calls](examples/03_atomic.py) follow the same rule. Async OpenAI clients expose async confirmation calls; atomic operations are available through `CoSensClient.atoms` (async) and `.sync_atoms` (sync). Keep stable idempotency keys across retries.

[API reference](docs/api-reference.md). Run tests with `PYTHONPATH=src pytest tests`.
