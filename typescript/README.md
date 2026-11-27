# CoSens TypeScript RSI SDK

Use Node.js 22.12 or later. Install the SDK from npm in your Agent project.

```sh
npm install cosens-sdk
```

The package is an ES module. CommonJS code can load it with `require('cosens-sdk/rsi')`, which Node.js supports for ES modules from 22.12. The entry points are `cosens-sdk` (everything), `cosens-sdk/rsi` and `cosens-sdk/memory`.

Cloud mode is the default. `new RSIClient()` uses the CoSens service with `COSENS_API_KEY` and uploads the supplied `evidence` and optional `recipe_reference`.

```ts
import { RSIClient } from 'cosens-sdk/rsi';

const cloud = new RSIClient();
const run = await cloud.execution_create({
  evidence: { context_path: 'context.txt', files: [{ path: 'sales.csv', role: 'input' }] },
  idempotency_key: 'delivery-42',
});
```

All methods are async in both modes. `new RSIClient({ mode: 'cloud', ... })` takes `api_key`, `base_url`, `timeout` in seconds (default 60) and `virtualUserId`. Cloud RSI uses `COSENS_RSI_BASE_URL` or defaults to `https://friday-rsi-cn.stardust-ai.com/api/v1`. The SDK appends `/v1` to a configured API prefix when needed. Service errors are `RSIError` with the service's `code` and the HTTP `status`. For local mode, pass `data_dir` and your own model as below.

To run local RSI, pass `model_id`, `api_key`, and `base_url` to the constructor. The model must support Chat Completions and tool calls. Use `config.headers` to authorize cloud Recipe downloads and uploads from a local client. Local `execution_create()` can download a missing published Recipe before queuing an update, and `recipe_reference.recipe_version_id` can pin the baseline. If you package a standalone Node with the host, set `RSI_NODE_EXECUTABLE` and include the SDK worker and the matching `better-sqlite3` native dependency.

```ts
import { RSIClient } from 'cosens-sdk/rsi';

const rsi = new RSIClient({
  data_dir: './rsi-data',
  model_id: 'your-model-id',
  api_key: process.env.RSI_MODEL_API_KEY!,
  base_url: 'http://127.0.0.1:8000/v1',
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

The SDK submits the snapshots and queues the Execution in one call. The Worker reruns the current task in a fresh workspace using its submitted `input` files and scores the new output with the Rubric. An active Execution can append evidence through `execution_append_evidence(id, evidence)`. The hook returns an Execution ID after queuing. Use `task_list()` for task cards, `task_get(taskId)` for clarification history and the final ZIP, and `recipe_list()` for choosing an available Recipe. Read the selected `active_version_id` with `recipe_get(recipe_id, version_id)`, or call `recipe_get(recipe_id)` for the active version. Use `execution_get(id)` for questions and test results. The SDK automatically publishes a passing, non-provisional version and archives the previous one. Use `recipe_version_set_archived(id, { version_id, archived })` to archive or restore the latest qualified version.

When the current request comes from chat, use `evidence.input_text` on `execution_create()` in either mode. Pass the user text needed to replay the task, including the data, ordering, and requested filename. The SDK preserves the text as a UTF-8 `task-input.txt` input alongside any submitted input files. The complete conversation in `context_path` and the delivered `artifact` are learning evidence. Keep assistant messages, tool results, and delivered output out of `input_text`.

```ts
const run = await rsi.execution_create({
  idempotency_key: confirmationRef,
  evidence: {
    context_path: contextPath,
    input_text: userRequest,
    files: [{ path: deliveredTodoPath, role: 'artifact' }],
  },
});
```

On SDK 0.10.1, save the user request to a separate UTF-8 file and submit it in `evidence.files` with `role: 'input'` when creating the Execution.

Pass just `data_dir` for read-only clients. After rotating the model key, create a Client with the same data directory and `config.credential_scope` and the new `api_key`. `recover()` resumes that account's unfinished work with the new Client settings. Use `recover({ recipe_id: recipeId })` to recover only executions for that SDK Recipe. Close each client with `close()` when done. The worker uses LangGraph for recovery and sends direct model API requests from its nodes.

To restart a local task, call `task_restart(taskId, { idempotency_key: requestId, evidence })`. Omit `evidence` to run with the existing materials. The task keeps its card ID and starts a new Execution. Reuse the key when resending the same restart request.

`await rsi.task_get(taskId)` returns `recipe_version_id` and `recipe_zip_path` once the final ZIP is ready. Publish the task with `rsi.recipes_upsert({ task_id: taskId })`. Pass `recipe_id` in the options to target an existing cloud Recipe. The upload uses the client's `config.headers` and the destination set by `COSENS_RSI_BASE_URL`, which defaults to the CoSens endpoint. A cloud client takes a ZIP path with `cloud.recipes_upsert(path)`.

Task publication includes the Rubrics used to approve that version, when available, and omits local evidence references. A cloud ZIP upload can supply Rubrics explicitly with `cloud.recipes_upsert(path, { rubrics })`. A pending local upload retains the Rubrics for retries.

`await cloud.recipe_download(recipeId, recipeVersionId)` returns the temporary signed download URL as a string. Fetch that URL to retrieve the ZIP.

Use `await rsi.rubrics_get(recipeId, recipeVersionId)` in local or cloud mode to read the Rubrics used by a specific Recipe version. The response includes `id` and `selected_version`. An imported Recipe version without associated Rubrics returns `not_found`.

See the [complete TS and Python interface reference](https://github.com/PreSeen/CoSens-SDK/blob/main/python/docs/local-rsi.md) and the [CoSens docs](https://cosens.ai/docs/). Run `npm run check` to build, typecheck and test the public package.

### Virtual User selection

For cloud RSI, use `new RSIClient({ mode: 'cloud', virtualUserId: 'vu_...' })` to bind this client to a Virtual User. Use separate clients for separate Virtual Users. The option sets the `X-Virtual-User-Id` request header, which is how the service identifies the Virtual User. `virtualUserId` is the only spelling; `virtual_user_id` is not an option and is rejected at compile time (a type error) and at runtime (a `TypeError` before any request is sent), here and in every Memory call below. Local RSI does not accept this option. Memory's `memoryRecall`, `memoryIngest`, `getMemoryItems`, and `getMemoryJob` take `virtualUserId` in their per-call options. `getMemory` and `getMemoryExperience` do not support Virtual User selection and reject it. Calls without a selection use the authenticated account. The server checks each caller's current grant; availability depends on the service deployment.

For a different Memory deployment, pass both `baseUrl` (the HTTPS Memory API URL, including its deployment-specific path) and `apiKey` to any Memory function: `memoryRecall`, `memoryIngest`, `getMemoryItems`, `getMemoryJob`, `getMemoryExperience`, or `getMemory`. For example, `await memoryRecall({ query: '...', baseUrl: 'https://memory.example/memory', apiKey: '...' })`. The explicit key is used only for that call and must be a non-empty string; invalid explicit keys never fall back to the environment key. The configured URL cannot contain credentials, a query, a fragment, or path traversal, and redirects are not followed.
