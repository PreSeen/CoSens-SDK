# CoSens SDK

Python and TypeScript clients for the current CoSens Task/Execution/Revision API. All three entry points record execution without generating a Recipe:

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

After the host obtains user confirmation of the artifact, call `generate_recipe` / `generateRecipe`, or the atomic Recipe endpoint. That single endpoint creates V1 or updates the associated Recipe. Feedback is recorded separately; attribution happens inside explicit generation. Generation has no Memory option: it recalls related Memory only when the execution behind the revision was started with `recall_memory` / `recallMemory`. Feedback carries a `source` (`human`, `evaluator`, `reference`, `automated_check` or `downstream`).

The `auto` mode selects useful existing context and records the task. It does not authorize Recipe generation. Submission, feedback, Session ACK, polling and recovery do not initiate a new generation request.

See [Python](python/README.md), [TypeScript](typescript/README.md), and the three examples in each package. This is a breaking protocol change: Run, standalone attribution, acceptance and learning-job APIs have been removed.

Tests: `PYTHONPATH=python/src pytest python/tests` and `npm --prefix typescript run check`. Optional real local HTTP integration: set `COSENS_TEST_ALGORITHM_HTTP_URL=http://127.0.0.1:18149` against the algorithm repository's `tests/support/explicit_http_server.py` fixture. It uses PostgreSQL and deterministic model substitutes, not a production model or production data.

## One API Key

Set `COSENS_API_KEY` to your CoSens API Key. The SDK uses the US CoSens API; `COSENS_REGION` defaults to `us` and accepts only `us`. Your application passes the same key as the model client's `api_key` and sets `COSENS_MODEL_BASE_URL` to `https://llm.cosens.ai/v1`. The SDK itself reads only `COSENS_API_KEY` and `COSENS_REGION`; `RSI_API_KEY*`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`, `COSENS_MODE` and `COSENS_INPUT` are not SDK configuration. The application loads any `.env` file.

Pass a public model ID in the model call. Algorithm operations accept only `model`, never a second API Key, base URL, auth type or provider settings. Model credentials are carried in Authorization. The algorithm gateway must forward that header alongside the authenticated user ID. Recipe generation still requires external artifact confirmation.
