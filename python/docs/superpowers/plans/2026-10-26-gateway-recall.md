# Gateway and turn recall implementation plan

Spec: `python/docs/2026-10-26-cosens-python-sdk-design.md` (current revision).

历史实施计划：以下鉴权描述记录当时的 Bearer-only 方案。当前请求头契约以设计文档第 1 节为准，CoSens 请求同时发送 `X-Auth-Type: api_key` 与 `Authorization: Bearer <api_key>`。

Continue on the user's `wangzhichao_sdk_2026-10-26` branch. Preserve unrelated `.idea/` files. Use HTTP mocks for writes; do not invent the missing standalone recall endpoint.

### Task 1: Private gateway transport

- [ ] Test sync/async requests retain `/api`, query strings, Friday API Key Bearer and idempotency headers, including administrator paths.
- [ ] Change the private service URL and service identity; retain native model URL behavior.
- [ ] Update HTTP fixtures to the gateway paths and run the suite.

Expected: `.venv/bin/python -m pytest -q` in `python/` passes after observing the new gateway test fail.

### Task 2: Freeze context once per turn

- [ ] Test transparent Responses continuation, explicit scope, conversation references, pre-execution retries, and native Agent approval resume preserve Recipe/Memory without repeated prepare/get_run or reclassification.
- [ ] Persist prepared Recipe/context and complete Run snapshot before native execution; reuse it in tool loops and resume. Preserve the existing native-result and Session ACK contracts.
- [ ] Reject incomplete/failed recall data before calling the business model; preserve valid empty snapshots.

Expected: targeted regression tests fail before changes; all tests pass afterwards.

### Task 3: Independent recall boundary and documentation

- [ ] For transparent disable, auto false and auto decision failure, replace silent passthrough with the independent recall path.
- [ ] Obtain the public non-Run recall HTTP contract. Until supplied, surface an explicit capability error before business model execution; do not guess an endpoint or create a Run.
- [ ] Expose the documented atomic recall capability boundary with the same explicit error while the contract is absent. Keep native non-generation APIs callable.
- [ ] Update usage documentation and implementation status honestly; verify tests and build.

Expected: no claim of working standalone remote recall without its contract. Successful standalone recall remains blocked pending backend details.

Gateway authentication follows `/Users/a1111/IdeaProjects/friday-rsi/docs/api/gateway-authentication.md`: only `api_key` / `RSI_API_KEY` is provided by the caller, sent as Authorization Bearer; no custom api-key header or backend service token.

## Review Focus

- No Run creation or cursor advance on disable/auto false; no model call after missing/failed required recall.
- Context is saved durably and cannot lose Recipe on a tool continuation.
- Streaming, raw response, sync and async paths observe the same pre-model boundary.
- Gateway prefix is preserved on ordinary and administrator requests; native model URL stays independent.
- Old direct-server state cannot resume against the new service accidentally.
