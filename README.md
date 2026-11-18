# CoSens Local RSI SDK

CoSens turns confirmed task evidence into reusable Recipes. The local SDK is available in TypeScript and Python. Both provide Material, Execution, Rubrics, and Recipe atomic functions.

```text
Host Hook → local SDK → SQLite queue → on-demand worker
→ extract user feedback → analyze evidence → generate candidate → execute test → score → save result
```

| Host | Import | Guide |
| --- | --- | --- |
| Node.js / TypeScript | `import { RSIClient } from 'cosens-sdk/rsi'` | [TypeScript](typescript/README.md) |
| Python | `from cosens.rsi import RSIClient` | [Python](python/docs/local-rsi.md) |

The host confirms the deliverable and selects the task trajectory. It creates a Material from local file paths, calls `execution_create`, then uses the returned ID to read status or answer clarification questions. The worker continues after the Hook returns.

Before deriving new Rubrics, the worker reads context and files with the `feedback` role in a dedicated pass. It extracts reusable, explicit user requirements and corrections with their evidence references. The analysis combines them with task requirements and execution evidence. The extracted rules are available in `execution_get` → `analysis.feedback`. Supplying a fixed Rubrics version skips extraction for Rubric generation.

When the learning evidence includes a correction, the worker records likely causes and their source references in `execution_get` → `analysis.attribution`. Recipe generation uses those findings to write reusable steps or checks. A successful `recipe_test` shows that the Recipe met the Rubrics on the test input.

Recipe and Rubric rules generalize historical examples into conditions, transformations and checks. The worker retries generation when it detects copied learning records. The evaluator receives only criteria, required flags and the pass threshold from the Rubric. Evidence references remain available for learning audits.

During Recipe analysis the worker asks for clarification only when the evidence leaves multiple plausible interpretations of a reusable rule and the choice changes a future outcome or the rule's applicability. The worker applies rules supported by the evidence directly and expresses differences that future inputs or established conditions can resolve as parameters or branches.

Use separate data directories for the two runtimes. Each owns its local SQLite queue, files and checkpoints.

Run the checks from each package directory with `npm ci && npm run check` or `python -m pip install '.[test]' && python -m pytest`.
