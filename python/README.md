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
| `COSENS_REGION` | SDK | `us` (default and only supported value) |
| `COSENS_MODEL_BASE_URL` | Your model client | `https://llm.cosens.ai/v1` |

The SDK reads the process environment; load any `.env` file in your application. The model IDs you can use are listed in the [CoSens documentation](https://cosens.ai/docs/).

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

In `auto` mode the SDK first makes a short structured decision call through your client (Chat Completions with a JSON schema `response_format`), so the model must support structured output. The call is made once, without retries, with a 5-second timeout and at most 64 output tokens. If the decision or the read-only Recipe lookup fails or times out, your request still runs without CoSens capture. A short request can be skipped (`activate: false`); a skipped turn makes no Recipe or Memory lookup and leaves no Task or Revision.

| Option | Default | Effect |
| --- | --- | --- |
| `decision_timeout` | `5` | Seconds allowed for the decision and Recipe-match calls |
| `decision_max_output_tokens` | `64` | Output cap for the decision and Recipe-match calls |
| `decision_client` | the wrapped client | Separate `OpenAI` / `AsyncOpenAI` client (matching the wrapped one) for those calls, e.g. a small fast model |
| `decision_extra_body` | none | Extra request body for those calls, e.g. `{"enable_thinking": False}` |
| `max_context_chars` | `4000` | Upper bound for the Recipe/Memory text added to a request or Agent instructions |

Reasoning models spend their output budget on reasoning, so a 64-token decision can be cut off and fall back to an uncaptured request. Disable reasoning for the decision with `decision_extra_body` (the field name depends on your gateway, for example `{"enable_thinking": False}` or `{"reasoning_effort": "low"}`), or pass a non-reasoning `decision_client` and `decision_model`.

```python
client = cosens(model_client, mode="auto", decision_extra_body={"enable_thinking": False})
```

When a usable Recipe matches, the SDK adds it as plain text: its title, when/when-not, numbered steps, related Memory text and, if room remains, lesson actions. The block never exceeds `max_context_chars`; a final line states what was omitted. A list with no usable Recipe is remembered for 60 seconds, so routine requests skip the lookup.

## Generate a Recipe after confirmation

Check that the turn has a submitted Revision before offering Recipe generation. After your application obtains user confirmation of that artifact, call from its confirmation handler:

```python
turn = client.cosens.get_turn(turn_id) if turn_id else None
if turn and turn.get("revision_id"):
    receipt = client.cosens.generate_recipe(turn_id, request_key="confirmed-report-1")
```

This is the only generation action. It creates the first Recipe version or updates the associated Recipe, always reads relevant Memory, and performs feedback attribution internally. `feedback` records feedback; it does not update a Recipe.

`client.cosens.wait_learning(turn_id)` raises `cosens.RSICandidateFailed` when generation fails. The error has `code`, `phase`, `retryable`, `upstream_status`, `upstream_code`, `candidate_id` and `request_id`. When `retryable` is true, `client.cosens.resume_learning(turn_id)` retries the same candidate. Older servers report only `code`; `retryable` is then `None`.

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

- Returned JSON objects are dicts with a `request_id` attribute holding the response's `x-request-id`, for support requests.
- `start_recipe_candidate(task_id, revision_id=...)` may omit `key`; the default is derived from the task, revision and `max_completion_tokens`, so repeating the call returns the same candidate.
- `wait_workflow_job("recipe_candidate", id)` raises `RSICandidateFailed` when the candidate failed.

## More

- Documentation: https://cosens.ai/docs/
- Source, examples and issues: https://github.com/PreSeen/CoSens-SDK
