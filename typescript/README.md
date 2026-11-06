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
| `COSENS_REGION` | SDK | `cn` (default) or `us`; the key must belong to that region |
| `COSENS_MODEL_BASE_URL` | Your model client | The OpenAI-compatible model gateway for your region; for `us` it is `https://llm.cosens.ai/v1` |

The SDK reads the process environment; load any `.env` file in your application. The model gateway for other regions and the model IDs you can use are listed in the [CoSens documentation](https://cosens.ai/docs/).

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

In `auto` mode the SDK first makes a short structured decision call through your client (Chat Completions with a JSON schema `response_format`), so the model must support structured output. The call uses a 60-second timeout by default; tune it with `decisionTimeoutMs`. If the decision or the read-only Recipe lookup fails, your request still runs without CoSens capture.

## Generate a Recipe after confirmation

From your application's artifact-confirmation handler:

```ts
const turn = client.cosens.getTurn(turnId);
if (turn?.revisionId) {
  const receipt = await client.cosens.generateRecipe({ turnId, requestKey: 'confirmed-report-1' });
}
```

Submission and feedback only record evidence. Explicit generation creates or updates the associated Recipe, recalls related Memory by default and analyzes feedback internally. There is no generation `includeMemory` option.

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

Use `CoSensClient.atoms` for Task, Execution, Revision, feedback, Recipe, Session and Memory operations. Keep the same request key when retrying an uncertain response.

Recipe token budgets can be supplied as `maxCompletionTokens`:

```ts
await client.atoms.startRecipeCandidate(taskId, {
  revisionId, key: 'confirmed-v1', maxCompletionTokens: 200000,
});
await wrapped.cosens.generateRecipe({ turnId, maxCompletionTokens: 200000 });
await client.atoms.resumeRecipeCandidate(candidateId, { maxCompletionTokens: 300000 });
```

The value must be an integer from 1 to 1,000,000. New candidates default to 200,000, and the setting is stored per candidate for attribution and Recipe generation. Resume preserves it unless explicitly overridden. Upstream model limits still apply. The positional form `resumeRecipeCandidate(id, model)` remains supported.

## More

- Documentation: https://cosens.ai/docs/
- Source, examples and issues: https://github.com/PreSeen/CoSens-SDK
