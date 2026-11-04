# CoSens SDK

Python and TypeScript clients for the current CoSens Task/Execution/Revision API. All three entry points record execution without generating a Recipe:

| Entry | Python | TypeScript |
| --- | --- | --- |
| Transparent OpenAI | `cosens(OpenAI())` | `cosens(new OpenAI())` |
| Explicit Agent execution | `cosens(agent).run(agent, input)` | `cosens(agent).run(agent, input)` |
| Atomic HTTP operations | `client.atoms` / `client.sync_atoms` | `client.atoms` |

After the host obtains user confirmation of the artifact, call `generate_recipe` / `generateRecipe`, or the atomic Recipe endpoint. That single endpoint creates V1 or updates the associated Recipe. Feedback is recorded separately; attribution happens inside explicit generation. Generation always recalls related Memory and has no `include_memory` option.

The `auto` mode selects useful existing context and records the task. It does not authorize Recipe generation. Submission, feedback, Session ACK, polling and recovery do not initiate a new generation request.

See [Python](python/README.md), [TypeScript](typescript/README.md), and the three examples in each package. This is a breaking protocol change: Run, standalone attribution, acceptance and learning-job APIs have been removed.

Tests: `PYTHONPATH=python/src pytest python/tests` and `npm --prefix typescript run check`. Optional real local HTTP integration: set `COSENS_TEST_ALGORITHM_HTTP_URL=http://127.0.0.1:18149` against the algorithm repository's `tests/support/explicit_http_server.py` fixture. It uses PostgreSQL and deterministic model substitutes, not a production model or production data.
