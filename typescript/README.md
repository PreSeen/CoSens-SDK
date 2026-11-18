# CoSens TypeScript Local RSI SDK

Use Node.js 22 or later. Install the tarball from your Agent project and import the ESM entry point.

```sh
npm install /path/to/cosens-sdk-<version>.tgz
```

Use `import { RSIClient } from 'cosens-sdk/rsi'` in the host. To work from the SDK source, run `npm ci` in this directory.

The model must support Chat Completions and tool calling. Set `RSI_MODEL_API_KEY` in the host process environment. The example assumes a local model endpoint. For packaging, include `dist/rsi/worker.js` and the matching `better-sqlite3` native dependency. The worker defaults to the current Node executable. A host with a bundled standalone Node can set `node_executable`.

```ts
import { RSIClient } from 'cosens-sdk/rsi';

const rsi = new RSIClient({
  data_dir: appDataDirectory,
  config: {
    model: { model: 'your-local-model', base_url: 'http://127.0.0.1:8000/v1' },
    max_executions: 2,
    max_model_requests: 1,
  },
});

async function onConfirmedDelivery(delivery: {
  sessionId: string;
  deliveryId: string;
  confirmationRef: string;
  contextPath: string;
  inputPath: string;
  outputPath: string;
  baseRecipeVersionId?: string;
}) {
  const key = JSON.stringify([delivery.sessionId, delivery.deliveryId]);
  const material = rsi.material_create({
    manifest: {
      context_path: delivery.contextPath,
      source_session_id: delivery.sessionId,
      capture_boundary: delivery.deliveryId,
      files: [
        { path: delivery.inputPath, role: 'input' },
        { path: delivery.outputPath, role: 'artifact' },
      ],
    },
    idempotency_key: key,
  });
  const execution = await rsi.execution_create({
    material_ids: [material.id],
    source_session_id: delivery.sessionId,
    base_recipe_version_id: delivery.baseRecipeVersionId,
    idempotency_key: key,
    policy: {
      artifact_confirmed: true,
      confirmation_ref: delivery.confirmationRef,
      test_task: {
        instruction: 'Use the test input to produce summary.md.',
        input_locators: [material.files[1].locator],
        shared_reference_locators: [],
        checks: [{ kind: 'exists', path: 'summary.md' }],
      },
    },
  });
  return execution.id;
}
```

The Hook returns after the task is queued and a worker is dispatched. The host queries `execution_get(id)` for stage, questions and results. Display required questions in the host UI and pass answers to `execution_clarification_answer`. Call `recover()` once on host startup to pick up unfinished work. If worker leases still occupy all slots, `scheduled: true` indicates that a detached process will wait for a slot and take over after expiry. Call `close()` when the host is done with the client.

| Object | Atomic functions |
| --- | --- |
| Material | `material_create`, `material_get`, `material_list`, `material_update` |
| Execution | `execution_create`, `execution_get`, `execution_list`, `execution_update`, `execution_cancel`, `execution_clarification_answer` |
| Rubrics | `rubrics_create`, `rubrics_get`, `rubrics_list`, `rubrics_update`, `rubrics_version_create`, `rubrics_version_list` |
| Recipe | `recipe_create`, `recipe_get`, `recipe_list`, `recipe_update`, `recipe_version_create`, `recipe_version_list`, `recipe_test`, `recipe_activate` |

Use `archived` with update functions to archive or restore records. Recipe retrieval defaults to the active version. Test results live on the Execution. A missing test task yields `unverifiable`. Activation requires a passing test and no unresolved clarification. The host owns task boundary selection and the learning confirmation UI.

Each client can supply its own `config` when using the same `data_dir`. Execution creation saves that configuration on the Execution, and the Worker reads it from there. Opening a client for reads or recovery can omit `config`.

SQLite records live in `rsi-ts.sqlite3` under `data_dir`. Immutable material snapshots, test files and graph checkpoints are stored alongside it. Use a separate directory for each runtime. Background workers exit after the queue drains. The tested release environment is Linux with standalone Node.

Use `test_task.input_locators` to authorize the current test files and `shared_reference_locators` to authorize reusable reference files. Each locator must come from an attached Material and match its respective `input` or `reference` role. Omitted lists authorize zero files. The learning stage gets historical evidence and excludes the declared test inputs. The evaluator uses the same authorized test files plus this run's outputs and trace.

Set `COSENS_RSI_WEB_SEARCH_API_KEY` to an Exa key to enable the optional `web_search` tool during Recipe generation. Domain filters are comma-separated hostnames in `COSENS_RSI_WEB_SEARCH_ALLOWED_DOMAINS` and `COSENS_RSI_WEB_SEARCH_BLOCKED_DOMAINS`. Search results appear in the generation trace.

Run `npm run check` to build, typecheck and test the packaged entry point using a local model protocol fixture.
