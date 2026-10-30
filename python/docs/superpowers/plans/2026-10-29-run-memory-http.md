# Run and Memory HTTP implementation plan

Source: `RSI_WORKFLOW_API(3).md` and `python/docs/2026-10-28-task-workflow-sdk-design.md` §8. CN OpenAPI snapshot on 2026-10-29 confirms Memory items and guided Run paths. The upload-grant path is absent from the deployed schema.

1. Add contract tests for Python async/sync and TypeScript public atoms: Memory source revision fields, bounded recall/items, guided Run request bodies and paths, and authentication headers.
2. Implement the tested public atoms. Preserve the established SDK ingest defaults (`decision`, `true`) for existing callers, document them as explicit SDK values, and allow callers to pass the service defaults (`message`, `false`). Keep Task and assist flows independent.
3. Verify the CN gateway read-only capabilities and run all offline Python and TypeScript checks. After confirming deployment, exercise the user-requested integration tests with uniquely marked synthetic Task, Recipe, guided Run and Memory records; preserve receipts and do not overwrite existing business data.
4. Update SDK documentation with implemented methods and distinguish available deployed routes from the upload-grant route awaiting a schema. The one-time upload adapter requires the grant response fields, target URL rules, and safe authentication scope; do not invent these.

Status on 2026-10-29: steps 1–4 completed for Memory and guided Run. Python 238 tests and TypeScript 145 tests/build/typecheck pass. CN Task smoke, guided step submission/completion/events, Memory source updates/idempotency/conflicts pass. The latest Memory extraction job is still pending. The user subsequently confirmed the downstream rollout is incomplete and requested implementation now. The upload adapter is implemented under `2026-10-29-document-upload.md`; live grant verification remains pending deployment (OPTIONS 404).
