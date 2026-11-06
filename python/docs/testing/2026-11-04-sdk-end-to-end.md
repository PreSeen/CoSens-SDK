# Python SDK real US end-to-end check — 2026-11-04

Reference: https://cosens.ai/docs/ and this repository's `python/README.md`.
The test used isolated Python 3.12 environments. One installed the local `python/` package with `pip install './python[agents,examples,test]'`; the other installed the Quickstart's published PyPI package with `pip install 'cosens-sdk[agents]==0.4.1'`. Both installed version `0.4.1`, and all 23 Python module files in the two installations had identical SHA-256 digests. The process mapped the existing US gateway credential to `COSENS_API_KEY`; no key was written to this report or the repository.

| Check | Result |
| --- | --- |
| Clean installation and imports | Passed |
| Published PyPI package, version and module parity | Passed; `cosens-sdk[agents]==0.4.1`, 23/23 Python modules identical to the local source install |
| SDK environment and client initialization | Passed; US service selected and SDK/model clients used the same key |
| Real CoSens Task list | Passed |
| Real model catalog | Passed; 18 models available |
| Direct Responses request (`deepseek-flash`) | Passed |
| Official Python tests (`PYTHONPATH=src pytest tests`) | 128 passed, 3 skipped; skipped cases require the optional local PostgreSQL algorithm fixture |
| Transparent Responses wrapper | Passed after raising decision timeout from 5 to 30 seconds; the model decided not to capture the one-off sentence, so no Revision was created |
| Agents SDK wrapper | Passed; Task `task_d8832c603bf64604bf7d308147386325`, Revision `revision_670ec0e0d4c940e3a5346b96e00c9909` |
| Atomic Task → Execution → Artifact → Revision | Passed; Task `task_017fcf86a46a4c279ea424459d55aef2`, Revision `revision_300c94283de845169d761ce35934c8a0` |
| `start_recipe_candidate` without `include_memory` | Request accepted; candidate `recipe_candidate_cfaa325b3cf74ecea5fc6691e9899557` returned `queued` |
| Candidate processing | Failed in server phase `generating` with `error: RuntimeError`; `memory_context: captured` |
| Resume same Candidate with `model='deepseek-flash'` | Request accepted and queued; server again reported `failed`, phase `generating`, `RuntimeError` |
| Published-package same-key Recipe replay | Failed with server error `task model is frozen; only its API Key may rotate`; no new Candidate was reported |

The initial transparent wrapper attempt timed out during the decision model call and left only a local `pre_execution_pending` state; it created no Task or Revision. The successful retry produced a nonempty model answer, but the decision was `activate: false` for this one-off task. The atomic path used that real model output as the confirmed artifact.

The Recipe failure is after successful request validation and queuing. The public candidate response exposes only `RuntimeError`, so the underlying server exception cannot be determined from the SDK response. A later replay through the published package, with the original task, revision and idempotency key, returned a separate task-model freeze error after the Candidate had been resumed with an explicit model. Investigate server logs for candidate `recipe_candidate_cfaa325b3cf74ecea5fc6691e9899557` and Revision `revision_300c94283de845169d761ce35934c8a0`.
