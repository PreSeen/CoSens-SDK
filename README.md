# CoSens Local RSI SDK

CoSens turns confirmed task evidence into reusable Recipes. The local SDK is available in TypeScript and Python. Both provide Material, Execution, Rubrics, and Recipe atomic functions.

```text
Host Hook → local SDK → SQLite queue → on-demand worker
→ analyze evidence → generate candidate → execute test → score → save result
```

| Host | Import | Guide |
| --- | --- | --- |
| Node.js / TypeScript | `import { RSIClient } from 'cosens-sdk/rsi'` | [TypeScript](typescript/README.md) |
| Python | `from cosens.rsi import RSIClient` | [Python](python/docs/local-rsi.md) |

The host confirms the deliverable and selects the task trajectory. It creates a Material from local file paths, calls `execution_create`, then uses the returned ID to read status or answer clarification questions. The worker continues after the Hook returns.

When the learning evidence includes a correction, the worker records likely causes and their source references in `execution_get` → `analysis.attribution`. Recipe generation uses those findings to write reusable steps or checks. A successful `recipe_test` shows that the Recipe met the Rubrics on the test input.

Recipe and Rubric rules generalize historical examples into conditions, transformations and checks. The worker retries generation when it detects copied learning records. The evaluator receives only criteria, required flags and the pass threshold from the Rubric. Evidence references remain available for learning audits.

Use separate data directories for the two runtimes. Each owns its local SQLite queue, files and checkpoints.

Run the checks from each package directory with `npm ci && npm run check` or `python -m pip install '.[test]' && python -m pytest`.
