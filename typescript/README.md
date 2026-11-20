# CoSens TypeScript RSI SDK

Use Node.js 22 or later. Install the SDK tarball from your Agent project.

```sh
npm install /path/to/cosens-sdk-<version>.tgz
```

Cloud mode is the default. `new RSIClient()` uses the CoSens service with `COSENS_API_KEY` and uploads the supplied `evidence` and optional `recipe_reference`.

```ts
import { RSIClient } from 'cosens-sdk/rsi';

const cloud = new RSIClient();
const run = await cloud.execution_create({
  evidence: { context_path: 'context.txt', files: [{ path: 'sales.csv', role: 'input' }] },
  idempotency_key: 'delivery-42',
});
```

All methods are async in both modes. For local mode, pass `data_dir` and your own model as below.

In local mode, pass the host's model request headers in `config.headers`. The model must support Chat Completions and tool calls. If you package a standalone Node with the host, set `RSI_NODE_EXECUTABLE` and include the SDK worker and the matching `better-sqlite3` native dependency.

```ts
import { RSIClient } from 'cosens-sdk/rsi';

const rsi = new RSIClient({
  data_dir: './rsi-data',
  config: {
    model: 'your-model-id',
    base_url: 'http://127.0.0.1:8000/v1',
    headers: {},
    auto_start: true,
  },
});
await rsi.recover();

async function onConfirmedDelivery(
  contextPath: string,
  currentInputPath: string,
  confirmationRef: string,
) {
  const run = await rsi.execution_create({
    evidence: {
      context_path: contextPath,
      files: [{ path: currentInputPath, role: 'input' }],
    },
    idempotency_key: confirmationRef,
  });
  return run.id;
}
```

The SDK submits the snapshots and queues the Execution in one call. The Worker reruns the current task in a fresh workspace using its submitted `input` files and scores the new output with the Rubric. An active Execution can append evidence through `execution_append_evidence(id, evidence)`. The hook returns an Execution ID after queuing. Use `recipe_list()` for the panel and for choosing an available Recipe. Read the selected `available_version_id` with `recipe_get(recipe_id, version_id)`. Use `execution_get(id)` for questions and test results. The SDK automatically publishes a passing, non-provisional version and archives the previous one. Use `recipe_version_set_archived(id, { version_id, archived })` to archive or restore the latest qualified version.

Use `config: null` for read-only clients. After a token refresh, create a Client with the same data directory and `credential_scope` and the new headers. `recover()` resumes that account's unfinished work with the new Client settings. Use `recover({ recipe_id: recipeId })` to recover only executions for that SDK Recipe. Close each client with `close()` when done. The worker uses LangGraph for recovery and sends direct model API requests from its nodes.

See the [complete TS and Python interface reference](../python/docs/local-rsi.md). Run `npm run check` to build, typecheck and test the public package.

### Virtual User selection

For cloud RSI, use `new RSIClient({ mode: 'cloud', virtualUserId: 'vu_...' })` to bind this client to a Virtual User. Use separate clients for separate Virtual Users. Local RSI does not accept this option. Memory's `memoryRecall`, `memoryIngest`, `getMemoryItems`, and `getMemoryJob` take `virtualUserId` in their per-call options. `getMemory` and `getMemoryExperience` do not support Virtual User selection and reject it. Calls without a selection use the authenticated account. The server checks each caller's current grant; availability depends on the service deployment.

For a different Memory deployment, pass both `baseUrl` (the HTTPS Memory API URL, including its deployment-specific path) and `apiKey` to `memoryRecall`, `memoryIngest`, `getMemoryItems`, or `getMemoryJob`. For example, `await memoryRecall({ query: '...', baseUrl: 'https://memory.example/v1/memory', apiKey: '...' })`. The explicit key is used only for that call and must be a non-empty string; invalid explicit keys never fall back to the environment key. The configured URL cannot contain credentials, a query, a fragment, or path traversal, and redirects are not followed.
