# CoSens TypeScript SDK

```ts
import { cosens, fridayGatewayClient } from 'cosens-sdk';

const client = cosens(fridayGatewayClient(), { mode: 'auto' });
const result = await client.responses.create({ model: '<public-model-id>', input: 'Produce a report' });
const turnId = client.cosens.lastTurnId;
```

Set `COSENS_API_KEY` and optionally `COSENS_REGION=cn|us` (default `cn`). Both clients reuse this key. Use a public model ID from the selected regional gateway.

`auto` uses a 60-second timeout for structured decisions by default; tune it with `decisionTimeoutMs`. If decision or read-only Recipe lookup fails, the native model request still runs without CoSens capture.

From the application's artifact-confirmation handler:

```ts
const turn = client.cosens.getTurn(turnId);
if (turn?.revisionId) {
  const receipt = await client.cosens.generateRecipe({ turnId, requestKey: 'confirmed-report-1' });
}
```

Submission and feedback only record evidence. Explicit generation creates or updates the associated Recipe, recalls related Memory by default and analyzes feedback internally. There is no generation `includeMemory` option.

See [transparent capture](examples/01_transparent.ts), [Agent execution](examples/02_explicit_run.ts), and [atomic calls](examples/03_atomic.ts). Use `CoSensClient.atoms` for Task, Execution, Revision, feedback, Recipe, Session and Memory operations. Keep the same request key when retrying an uncertain response.

`npm run check` runs the build, type checks and current tests. For real local algorithm HTTP tests, set `COSENS_TEST_ALGORITHM_HTTP_URL` as described in the repository README.

Recipe token budgets can be supplied as `maxCompletionTokens`:

```ts
await client.atoms.startRecipeCandidate(taskId, {
  revisionId, key: 'confirmed-v1', maxCompletionTokens: 200000,
});
await wrapped.cosens.generateRecipe({ turnId, maxCompletionTokens: 200000 });
await client.atoms.resumeRecipeCandidate(candidateId, { maxCompletionTokens: 300000 });
```

The value must be an integer from 1 to 1,000,000. The upgraded algorithm service defaults
new candidates to 200,000 and stores the setting per candidate for attribution and Recipe
generation. Resume preserves it unless explicitly overridden. Upstream model limits still
apply. The old positional model string on `resumeRecipeCandidate(id, model)` remains
supported. Older algorithm servers reject the new field; upgrade the server first.
