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

In `auto` mode the SDK first makes a short structured decision call through your client (Chat Completions with a JSON schema `response_format`), so the model must support structured output. The model writes only `{"activate": ..., "reason": ...}`, plus `"feedback": true|false` when the request follows an earlier recorded turn; the task objective is your user's text. The call is made once, without retries, with a 5-second timeout and a 1024-token output budget (reasoning included). If the decision or the read-only Recipe lookup fails or times out, your request still runs without CoSens capture, and the turn records why: `client.cosens.get_turn(client.cosens.last_turn_id)` has `capture == "decision_failed"` with `decision["error_type"]` and `decision["error"]` (for example a timeout, a gateway error, or output cut off at the token limit). A request without user text is skipped without a model call (`capture == "no_user_text"`).

| Option | Default | Effect |
| --- | --- | --- |
| `decision_timeout` | `5` | Seconds allowed for the decision and Recipe-match calls |
| `decision_max_output_tokens` | `1024` | Output budget, reasoning included, for the decision and Recipe-match calls |
| `decision_client` | the wrapped client | Separate `OpenAI` / `AsyncOpenAI` client (matching the wrapped one) for those calls, e.g. a small fast model |
| `decision_extra_body` | none | Extra request body for those calls, e.g. `{"enable_thinking": False}` |
| `max_context_chars` | `4000` | Upper bound for the Recipe/Memory text added to a request or Agent instructions |

Both models above reason before answering, which is why the budget is not smaller. Reasoning models that think longer can still hit the timeout or the token budget; the turn then records `decision_failed` with the cause. Use a small non-reasoning `decision_model` (or a separate `decision_client`), raise `decision_timeout` / `decision_max_output_tokens`, or switch reasoning off with `decision_extra_body` if your gateway accepts such a field (vLLM accepts `{"chat_template_kwargs": {"enable_thinking": False}}`; the LiteLLM gateway above rejects it with HTTP 500, which also records `decision_failed`).

Measured on 2026-11-06 with the SDK's own decision request (output tokens include reasoning):

| Model (gateway) | Decision tokens p50 / max | With linked feedback, max | Success at defaults | Decision latency p50 / max |
| --- | --- | --- | --- | --- |
| `deepseek-flash` (llm.cosens.ai) | 50 / 139 | 294 | 24 / 24 | 1.6 s / 3.7 s |
| `qwen3.8-27b` (vLLM via LiteLLM) | 189 / 469 | 337 | 23 / 24 (one 5 s timeout) | 1.6 s / 5.0 s |

The default budget leaves more than 3x headroom over the largest reply measured on the default model.

If the decision activates, a Recipe-match call picks a usable Recipe with the same limits. A match that times out or is cut off does not stop the capture: the turn is recorded without a Recipe and `turn["recipe_match_error"]` says why. With 20 usable Recipes, `deepseek-flash` matched within 5 s in 9 of 12 measured calls.

```python
client = cosens(model_client, mode="auto", decision_model="<small-non-reasoning-model-id>")
```

When a usable Recipe matches, the SDK adds it as plain text: its title, when/when-not, numbered steps, related Memory text and, if room remains, lesson actions. The block never exceeds `max_context_chars`; a final line states what was omitted. A list with no usable Recipe is remembered for 60 seconds, so routine requests skip the lookup.

## Generate a Recipe after confirmation

A turn has something to learn from only when it recorded a completed execution. After your application obtains user confirmation of that artifact, call from its confirmation handler:

```python
turn = client.cosens.get_turn(turn_id) if turn_id else None
if turn and turn.get("state") == "completed" and turn.get("execution_id"):
    client.cosens.generate_recipe(turn_id)
    record = client.cosens.wait_learning(turn_id)
```

`generate_recipe` learns the first Recipe version, or the next one, from the turn's execution; an execution learns once. `wait_learning` raises `cosens.RSILearningFailed` when the learning failed and `cosens.RSILearningStopped` when it stopped in a state only you can change; both are raised after the turn's record is stored, so `client.cosens.resume_learning(turn_id)` can continue it. `feedback(message, source=...)` records feedback on the turn's execution. The learning rules, statuses and errors are in [the API reference](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/api-reference.md); how a follow-up message becomes a correction is in [execution capture](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/unified-auto.md).

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

## Run RSI locally

Local RSI keeps Tasks, traces, feedback and Recipe versions on your machine and generates Recipes with your own model
quota. CoSens is used only for Memory recall and to store finished Recipe versions, so other machines can find them;
pass `client=None` to run without it. Python only for now.

```bash
pip install 'cosens-sdk[local]'
```

```python
from openai import AsyncOpenAI
from cosens import CoSensClient
from cosens.local import ChatCompletionsModel, CodexCLIModel, LocalRSI

rsi = LocalRSI(client=CoSensClient(), model=ChatCompletionsModel(AsyncOpenAI(base_url=..., api_key=...), "model-id"))
# Or use your Codex login: LocalRSI(client=CoSensClient(), model=CodexCLIModel())
```

`LocalRSI` takes the same task and new-execution shapes as `client.atoms` and keeps its earlier in-process names for
corrections and learning; its methods, its evidence references and the one-time store upgrade are described in
[Local RSI](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/local-rsi.md). Each new version is uploaded
without its evidence references. To continue a Recipe on another machine, find it with `search_recipes` or
`list_recipes`, read it with `get_recipe` and pass it as `create_task(..., recipe=...)`. See `examples/04_local_rsi.py`.

### Web search while learning a Recipe

`atoms.learn(execution_id, web_search=..., web_search_blocked_domains=["example.com"])` sends both fields in the
`POST /executions/{id}/learn` body, only when given. Omitted means the service default: on when the service has a search
provider. `web_search=False` opts out, `web_search_blocked_domains` (at most 50 bare hostnames) adds hosts whose pages are
dropped, and `web_search=True` on a service without a provider fails the learning with `web_search_unavailable` (not
retryable). The task runner's `generate_recipe(..., web_search=..., web_search_blocked_domains=[...])` passes them on;
the OpenAI wrapper's `generate_recipe` takes no web fields, so call `atoms.learn` for those turns.

- The setting is frozen on the learning, blocked hosts lowercased, deduplicated and sorted. `resume_learning` takes no
  web fields and uses the frozen one.
- Learning again compares only the web fields that call gives, blocked hosts as a set; giving none returns the learning.
  A different setting gets a 422 while the learning is active, `completed` or `awaiting_material`. On a `failed`,
  `interrupted` or `awaiting_runner` learning it runs the same version again with the new setting and drops the sources
  frozen under the old one, so `learn(..., web_search=False)` recovers a learning that failed with a non-retryable
  `web_search_unavailable`.
- Retries and resumes reuse the frozen sources and never search again; a provider failure fails the learning as
  `web_search_unavailable` (retryable), and the resume searches again.
- The learning's `web_search` lists the queries, the sources used (URL, title, excerpt, `retrieved_at`, `text_sha256`) and
  the dropped results.

`LocalRSI` searches the same way through a search you supply. Pass `LocalRSI(..., web_search=<a WebSearch>)`
(`cosens.local.WebSearch`: `async search(query, *, blocked_domains) -> list of {url, title, text}`) and each local learning
searches the web while its Recipe is written; the SDK ships no search client, and without one nothing is searched. Its
learning call, `start_recipe_candidate(..., web_search=..., web_search_blocked_domains=[...])`, takes the same settings, and
`web_search=True` with no `WebSearch` fails the candidate with `web_search_unavailable`.

- `LocalRSI(web_search_blocked_domains=[...])` blocks hosts for every candidate, as the service's own blocklist does;
  `web_search=False` still opts one candidate out, and only blocked domains passed with it raise `ValueError`.
- A revision has one candidate, as an execution has one learning, and its blocked hosts (with the instance blocklist)
  are frozen lowercased, deduplicated and sorted. A second `start_recipe_candidate` for the revision compares only the
  web fields it gives, blocked hosts as a set with the instance blocklist: the same setting or none returns the
  candidate; a different one raises `ValueError` while the candidate is in flight, finished or `awaiting_material`, and
  runs a `failed` or `interrupted` candidate again on the same version with the new setting, dropping the sources frozen
  under the old one. `resume_recipe_candidate` takes no web fields and keeps the frozen setting.
- The model writes at most 3 general queries from the task objective and the attribution, never from execution outputs,
  reference answers or web text. **Queries leave this machine to whatever provider your `WebSearch` calls**, so keep
  names, ids and secrets out of the task objective.
- Results are cut to 5 per query, 1,500 characters each and 24,000 in total, and dropped when the parsed URL host is a
  blocked host or one of its subdomains. Blocking lowers but does not remove the chance of copied or leaked content, so pass
  the hosts of any benchmark or private source a Recipe must not read.
- The candidate's `web_search` has the same shape as the service's. Retries and `resume_recipe_candidate` reuse frozen
  sources; a provider failure raises `WebSearchUnavailable` from your `WebSearch` and fails the candidate as
  `web_search_unavailable` (retryable).
- Web text is untrusted data, never instructions. A step or lesson that cites only web sources is rejected, so a page can
  refine a step that recorded evidence supports but cannot add one. The uploaded Recipe carries no evidence references,
  so the web provenance stays on the local candidate.
- `CodexCLIModel` runs `codex exec` with native `web_search` disabled, so only the search you supply is used.

## Atomic operations

`CoSensClient().atoms` (async) and `.sync_atoms` (sync) expose Task, Execution, feedback, learning, Recipe and Memory operations directly; see [the API reference](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/api-reference.md). Keep stable idempotency keys across retries.

A task is a capability that many executions reuse: `create_task` takes its `objective` and reference `artifacts`, and each `start_execution` takes `source` and that execution's own input (`objective`, `artifacts`, `self_contained`).

```python
task = await atoms.create_task(key="weekly-report", objective="Write the weekly management report")
execution = await atoms.start_execution(task["task_id"], key="run-1", model="<public-model-id>", source="new_task",
                                        objective="Report for week 46", artifacts=[notes_artifact_id])
result = await atoms.submit_execution(execution["execution_id"], request_sha256=execution["request_sha256"],
                                      outcome="completed", artifacts=[{"artifact_id": artifact_id, "role": "submitted_output"}])
if result["status"] == "completed":
    await atoms.learn(execution["execution_id"])
    learning = await atoms.wait_learning(execution["execution_id"])
```

A correction names only the execution it corrects: `start_execution(key="correction-1", previous_execution_id=execution_id)`.

Returned JSON objects are dicts with a `request_id` attribute holding the response's `x-request-id`, for support requests.

## More

- Documentation: https://cosens.ai/docs/
- Source, examples and issues: https://github.com/PreSeen/CoSens-SDK
