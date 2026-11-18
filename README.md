# CoSens SDK

[![PyPI](https://img.shields.io/pypi/v/cosens-sdk?label=PyPI)](https://pypi.org/project/cosens-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/cosens-sdk)](https://pypi.org/project/cosens-sdk/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Python and TypeScript clients for the current CoSens Task → Execution → Recipe API. All three entry points record execution without learning a Recipe:

| Entry | Python | TypeScript |
| --- | --- | --- |
| Transparent OpenAI | `cosens(model_client)` | `cosens(modelClient)` |
| Explicit Agent execution | `cosens(agent).run(agent, input)` | `cosens(agent).run(agent, input)` |
| Atomic HTTP operations | `client.atoms` / `client.sync_atoms` | `client.atoms` |

The SDK does not create model clients. Build the standard OpenAI client for your model gateway and pass it in:

```python
model_client = OpenAI(base_url=os.environ["COSENS_MODEL_BASE_URL"], api_key=os.environ["COSENS_API_KEY"])
client = cosens(model_client, mode="auto")
```

```ts
const modelClient = new OpenAI({ baseURL: process.env.COSENS_MODEL_BASE_URL, apiKey: process.env.COSENS_API_KEY });
const client = cosens(modelClient, { mode: 'auto' });
```

A Task is a capability, such as writing the weekly management report, that many executions reuse; each execution carries its own input, and each Recipe belongs to one Task. Learning needs feedback: record the user's words about the artifact as `human` feedback (`feedback` on the wrappers, `record_feedback` / `recordFeedback` on the atoms), then call `generate_recipe` / `generateRecipe` (or the atomic `learn`) to learn V1 or the next version of the Recipe the execution followed, once per execution. Without feedback on the execution or an earlier draft of it, learning is refused with `RSIFeedbackRequired` and nothing is created; a later version comes from learning another execution that followed the Recipe, either a new execution of the task or a correction of one (`previous_execution_id` / `previousExecutionId`). The `auto` mode only records and selects context; it never authorizes learning, and the [Python](python/README.md) and [TypeScript](typescript/README.md) READMEs describe the rest, including where the two SDKs differ.

See [Python](python/README.md), [TypeScript](typescript/README.md), and the three examples in each package. This is a breaking protocol change: Run, Revision, Recipe candidate, Session, standalone attribution, acceptance and learning-job APIs have been removed.

Tests: `PYTHONPATH=python/src pytest python/tests` and `npm --prefix typescript run check`. Optional real local HTTP integration: set `COSENS_TEST_ALGORITHM_HTTP_URL=http://127.0.0.1:18149` against the algorithm repository's `tests/support/explicit_http_server.py` fixture. It uses PostgreSQL and deterministic model substitutes, not a production model or production data.

## One API Key

Set `COSENS_API_KEY` to your CoSens API Key. The SDK uses the US CoSens API; `COSENS_REGION` defaults to `us` and accepts only `us`. Your application passes the same key as the model client's `api_key` and sets `COSENS_MODEL_BASE_URL` to `https://llm.cosens.ai/v1`. The SDK itself reads only `COSENS_API_KEY` and `COSENS_REGION`; `RSI_API_KEY*`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `COSENS_MODE` and `COSENS_INPUT` are not SDK configuration. The application loads any `.env` file.

Pass a public model ID in the model call. Algorithm operations accept only `model`, never a second API Key, base URL, auth type or provider settings. Model credentials are carried in Authorization. The algorithm gateway must forward that header alongside the authenticated user ID. Recipe generation still requires external artifact confirmation.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
