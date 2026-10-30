# Document upload implementation plan

The user explicitly requested implementation before the downstream rollout is complete. This supersedes the earlier deployment prerequisite in the SDK design §8.4. Delivery includes Python async/sync and TypeScript atoms, offline tests, and usage documentation; deployment verification remains a separately recorded pending check.

1. Add contract tests for grant creation, raw PUT bytes, ordinary multipart upload, status lookup, authentication, target URL validation, size/expiry checks, error redaction and no automatic retry.
2. Implement `create_document_upload_grant` / `createDocumentUploadGrant`, `upload_document_to_grant` / `uploadDocumentToGrant`, `upload_document` / `uploadDocument`, and `get_document_status` / `getDocumentStatus`.
3. Grant creation sends filename/mime_type and explicit optional metadata. Do not send caller-controlled user_id/graph_id. Use the attachment's route and the underlying Memory grant response fields (`upload_url`, `method`, optional `expires_at`, `max_bytes`) as the current adapter contract; the HTTP grant route itself is not yet deployed.
4. Accept only service-relative upload paths or HTTPS URLs at the configured regional gateway under `/api/v1/document/uploads/<token>`. Reject user info, query/fragment, foreign hosts/ports/regions, traversal and redirects. Require PUT, send bytes once, never execute the response's `curl`/`next_step` text, and never forward arbitrary response headers.
5. Keep atomic upload calls free of automatic retries. Hosts persist the grant/receipt and resolve uncertain outcomes before another upload; status lookup is available once a document ID is known. Server receipt fields remain unmodified JSON.
6. Run targeted tests, both full suites/build/typecheck, then update the SDK development document and API examples. Do not mark the undeployed grant endpoint as live-verified.

Status: all six implementation steps completed. Python 268 tests and TypeScript 152 tests pass, including 30/7 document upload tests. TypeScript build and typecheck pass. Live grant verification remains pending downstream deployment; no production upload success is claimed.
