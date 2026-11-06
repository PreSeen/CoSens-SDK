# CoSens Python SDK

CoSens records the tasks your application runs through an OpenAI client or an OpenAI Agents agent, and turns a confirmed result into a reusable Recipe.

## Install

```bash
pip install cosens-sdk
```

For OpenAI Agents support: `pip install 'cosens-sdk[agents]'`. Requires Python 3.10+.

## Configure

| Variable | Read by | Value |
| --- | --- | --- |
| `COSENS_API_KEY` | SDK and your model client | Your CoSens API Key |
| `COSENS_REGION` | SDK | `cn` (default) or `us`; the key must belong to that region |
| `COSENS_MODEL_BASE_URL` | Your model client | The OpenAI-compatible model gateway for your region; for `us` it is `https://llm.cosens.ai/v1` |

The SDK reads the process environment; load any `.env` file in your application. The model gateway for other regions and the model IDs you can use are listed in the [CoSens documentation](https://cosens.ai/docs/).

## Wrap an OpenAI client

Build a standard OpenAI client for your model gateway and pass it to `cosens(...)`:

```python
import os

from openai import OpenAI
from cosens import cosens

model_client = OpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"])
client = cosens(model_client, mode="auto")
result = client.responses.create(model="<public-model-id>", input="Produce a report")
turn_id = client.cosens.last_turn_id
```

The response is the native OpenAI result. `AsyncOpenAI` works the same way and exposes async CoSens calls.

In `auto` mode the SDK first makes a short structured decision call through your client (Chat Completions with a JSON schema `response_format`), so the model must support structured output. The call uses a 60-second timeout by default; pass `decision_timeout=<seconds>` to tune it. If the decision or the read-only Recipe lookup fails, your request still runs without CoSens capture. A short request can be skipped (`activate: false`), leaving no Task or Revision.

## Generate a Recipe after confirmation

Check that the turn has a submitted Revision before offering Recipe generation. After your application obtains user confirmation of that artifact, call from its confirmation handler:

```python
turn = client.cosens.get_turn(turn_id) if turn_id else None
if turn and turn.get("revision_id"):
    receipt = client.cosens.generate_recipe(turn_id, request_key="confirmed-report-1")
```

This is the only generation action. It creates the first Recipe version or updates the associated Recipe, always reads relevant Memory, and performs feedback attribution internally. `feedback` records feedback; it does not update a Recipe.

## Run an OpenAI Agents agent

```python
import os

from agents import Agent, set_default_openai_client
from openai import AsyncOpenAI
from cosens import cosens

set_default_openai_client(AsyncOpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"]))
agent = Agent(name="assistant", instructions="Complete the requested task.", model="<public-model-id>")
client = cosens(agent, mode="auto")
result = await client.run(agent, "Produce a report")
```

## Atomic operations

`CoSensClient().atoms` (async) and `.sync_atoms` (sync) expose Task, Execution, Revision, feedback, Recipe, Session and Memory operations directly. Keep stable idempotency keys across retries.

## More

- Documentation: https://cosens.ai/docs/
- Source, examples and issues: https://github.com/PreSeen/CoSens-SDK
