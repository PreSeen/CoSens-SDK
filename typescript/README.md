# CoSens TypeScript SDK

CoSens records the tasks your application runs through an OpenAI client or an OpenAI Agents agent, and turns a confirmed result into a reusable Recipe.

## Install

```bash
npm install cosens-sdk
```

Requires Node.js 22+. The package is ESM-only.

## Configure

| Variable | Read by | Value |
| --- | --- | --- |
| `COSENS_API_KEY` | SDK and your model client | Your CoSens API Key |
| `COSENS_REGION` | SDK | `us` (default and only supported value) |
| `COSENS_MODEL_BASE_URL` | Your model client | `https://llm.cosens.ai/v1` |

The SDK reads the process environment; load any `.env` file in your application. The model IDs you can use are listed in the [CoSens documentation](https://cosens.ai/docs/).

## Wrap an OpenAI client

Build a standard OpenAI client for your model gateway and pass it to `cosens(...)`:

```ts
import OpenAI from 'openai';
import { cosens } from 'cosens-sdk';

const modelClient = new OpenAI({ baseURL: process.env.COSENS_MODEL_BASE_URL, apiKey: process.env.COSENS_API_KEY });
const client = cosens(modelClient, { mode: 'auto' });
const result = await client.responses.create({ model: '<public-model-id>', input: 'Produce a report' });
const turnId = client.cosens.lastTurnId;
```

The response is the native OpenAI result.

In `auto` mode the SDK first makes a short structured decision call through your client (Chat Completions with a JSON schema `response_format`), so the model must support structured output. The model writes only `{ activate, reason }`; the task objective is your user's text. The call is made once (`maxRetries: 0`) with a 5-second timeout and a 1024-token output budget (reasoning included). If the decision or the read-only Recipe lookup fails or times out, your request still runs without CoSens capture, and the turn records why: `client.cosens.getTurn(client.cosens.lastTurnId)?.decision` has `capture: 'decision_failed'` with `error_type` and `error` (for example a timeout, a gateway error, or output cut off at the token limit). A request without user text is skipped without a model call (`capture: 'no_user_text'`).

| Option | Default | Effect |
| --- | --- | --- |
| `decisionTimeoutMs` | `5000` | Time allowed for the decision and Recipe-match calls |
| `decisionMaxOutputTokens` | `1024` | Output budget, reasoning included, for the decision and Recipe-match calls |
| `decisionClient` | the wrapped client | Separate OpenAI-compatible client for those calls, e.g. a small fast model |
| `decisionExtraBody` | none | Merged into those request bodies, e.g. `{ enable_thinking: false }` |
| `maxContextChars` | `4000` | Upper bound for the Recipe/Memory text added to a request or Agent instructions (also on `TaskWorkflowRunner`) |

Both models above reason before answering, which is why the budget is not smaller. Reasoning models that think longer can still hit the timeout or the token budget; the turn then records `decision_failed` with the cause. Use a small non-reasoning `decisionModel` (or a separate `decisionClient`), raise `decisionTimeoutMs` / `decisionMaxOutputTokens`, or switch reasoning off with `decisionExtraBody` if your gateway accepts such a field (vLLM accepts `{ chat_template_kwargs: { enable_thinking: false } }`; the LiteLLM gateway above rejects it with HTTP 500, which also records `decision_failed`).

Measured on 2026-11-06 with the SDK's own decision request (output tokens include reasoning):

| Model (gateway) | Decision tokens p50 / max | With linked feedback, max | Success at defaults | Decision latency p50 / max |
| --- | --- | --- | --- | --- |
| `deepseek-flash` (llm.cosens.ai) | 50 / 139 | 294 | 24 / 24 | 1.6 s / 3.7 s |
| `qwen3.8-27b` (vLLM via LiteLLM) | 189 / 469 | 337 | 23 / 24 (one 5 s timeout) | 1.6 s / 5.0 s |

The default budget leaves more than 3x headroom over the largest reply measured on the default model.

If the decision activates, a Recipe-match call picks a usable Recipe with the same limits. A match that times out or is cut off does not stop the capture: the turn is recorded without a Recipe and `turn.frozenContext.context.recipe_match_error` says why. With 20 usable Recipes, `deepseek-flash` matched within 5 s in 9 of 12 measured calls.

```ts
const client = cosens(modelClient, { mode: 'auto', decisionModel: '<small-non-reasoning-model-id>' });
```

When a usable Recipe matches, the SDK adds it as plain text: its title, when/when-not, numbered steps, related Memory text and, if room remains, lesson actions. The block never exceeds `maxContextChars`; a final line states what was omitted. A list with no usable Recipe is remembered for 60 seconds, so routine requests skip the lookup.

## Generate a Recipe after confirmation

A turn has something to learn from only when it recorded a completed execution. From your application's artifact-confirmation handler:

```ts
import { CoSensClient } from 'cosens-sdk';

const atoms = new CoSensClient().atoms;
const turn = client.cosens.getTurn(turnId);
if (turn?.state === 'completed' && turn.executionId) {
  await client.cosens.generateRecipe({ turnId });
  const learning = await atoms.waitLearning(turn.executionId);
}
```

`generateRecipe` learns the first Recipe version, or the next one, from the turn's execution; an execution learns once. `cosens.feedback(message, { source })` records feedback on the turn's execution. The learning rules, statuses and errors are in [the API reference](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/api-reference.md) (Python names; the TypeScript ones are camelCase).

### Differences from the Python SDK

- **No automatic feedback-to-correction.** The TypeScript wrappers do not classify a follow-up message as feedback on the previous turn. Record feedback with `cosens.feedback(...)`, then start the correction with a `CoSensClient`'s `start({ task_id, execution_id }, { phase: 'correct', requestKey })` or its `atoms.startExecution(undefined, { key, previousExecutionId })`.
- **No `waitLearning` / `resumeLearning` on the wrappers.** Use a `CoSensClient`'s `atoms.waitLearning(executionId)` and `atoms.resumeLearning(executionId)`; the agent runner also exposes them as `client.cosens.atoms`. The turn's `executionId` and its stored `learning` are on `getTurn(turnId)`.
- **The feedback key option is the other way round.** The transparent wrapper's `cosens.feedback(message, { source, turnId, requestKey })` accepts a `requestKey` and the agent runner's `cosens.feedback(message, source, turnId)` does not; in Python the agent runner's `feedback` takes `request_key` and the transparent wrapper's does not.

## Run an OpenAI Agents agent

```ts
import OpenAI from 'openai';
import { Agent, setDefaultOpenAIClient } from '@openai/agents';
import { CoSensAgentRunner } from 'cosens-sdk';

setDefaultOpenAIClient(new OpenAI({ baseURL: process.env.COSENS_MODEL_BASE_URL, apiKey: process.env.COSENS_API_KEY }));
const agent = new Agent({ name: 'assistant', instructions: 'Complete the requested task.', model: '<public-model-id>' });
const runner = new CoSensAgentRunner(agent, { mode: 'auto' });
const result = await runner.run(agent, 'Produce a report');
```

## Atomic operations

Use `CoSensClient.atoms` for Task, Execution, feedback, learning, Recipe and Memory operations. Each call sends one request. Keep the same key when retrying an uncertain response.

A task is a capability that many executions reuse: `createTask` takes its `objective` and reference `artifacts` (bare ids or `{ artifact_id, role: 'reference' }`), and each `startExecution` takes `source` and that execution's own input (`objective`, `artifacts`, `selfContained`).

```ts
import { CoSensClient } from 'cosens-sdk';

const client = new CoSensClient();
const task = await client.atoms.createTask({ key: 'weekly-report', objective: 'Write the weekly management report' });
const execution = await client.atoms.startExecution(task.task_id, { key: 'run-1', model: '<public-model-id>', source: 'new_task', objective: 'Report for week 46', artifacts: [notesArtifactId] });
const result = await client.atoms.submitExecution(execution.execution_id, { requestSha256: execution.request_sha256, outcome: 'completed', artifacts });
if (result.status !== 'completed') throw new Error('The execution result was not confirmed');
await client.atoms.recordFeedback(result.execution_id, { feedback: 'Use the Q3 numbers', source: 'human', key: 'feedback-1' });
const correction = await client.atoms.startExecution(undefined, { key: 'correction-1', previousExecutionId: result.execution_id });
```

A correction (`previousExecutionId`) takes only `model` besides, and a `taskId` is optional and must be the previous execution's task; `source`, an input field, `recipeId` or `recallMemory: true` with it fails to compile and throws before sending. A 422 `input_incomplete` or `recipe_choice_required` from the start is an `RSIHTTPError` whose `missing` or `recipeIds` names what the input lacks or the task's Recipes. Treat an execution as done only when the result's `status` is `'completed'`.

Learning is explicit and once per execution:

```ts
import { RSILearningFailedError, RSILearningStoppedError } from 'cosens-sdk';

await client.atoms.learn(executionId, { maxCompletionTokens: 200000 });
try {
  const learning = await client.atoms.waitLearning(executionId);
} catch (error) {
  if (error instanceof RSILearningFailedError && error.retryable) await client.atoms.resumeLearning(executionId);
  else if (error instanceof RSILearningStoppedError && error.status === 'interrupted') await client.atoms.resumeLearning(executionId);
  else throw error;
}
```

`learn` and `resumeLearning` send no key, and `waitLearning(executionId, { timeout })` takes seconds. When each error is thrown, and the 422 cases, are in [the API reference](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/api-reference.md). The TypeScript fields: `RSILearningFailedError` has `executionId`, `code`, `detailMessage` (the service's text; `message` is the error's own), `retryable` (`true`, `false` or `undefined`), `phase`, `upstreamStatus`, `upstreamCode` and `requestId`; `RSILearningStoppedError` has `learning`, `executionId`, `status` and `requestId`. A learning still in flight at the deadline throws an `Error` named `TimeoutError`.

The Recipe store: `uploadRecipe(recipeId, { version, recipe, previousVersion })`, `getRecipe(recipeId, { version })`, `listRecipes({ limit, cursor, origin })` and `searchRecipes(query, { limit })`. Only `generated` Recipes can be applied to an execution with `recipeId`, so the transparent wrapper's lookup offers only those.

A `CoSensClient`'s `start(snapshot, { phase })` takes `prepare` (an execution of `snapshot.task`, `{ source, objective, artifacts, self_contained }`, in `snapshot.task_id`, the task of a `recipeId`, or a new task named by its objective; needs `requestKey`), `correct` (a correction of `snapshot.execution_id`; needs `requestKey`) or `learn` (learns from `snapshot.execution_id`; no key). Returned objects have a non-enumerable `requestId` holding the response's `x-request-id`.

## More

- Documentation: https://cosens.ai/docs/
- Source, examples and issues: https://github.com/PreSeen/CoSens-SDK
