# CoSens Python SDK Implementation Plan

> Historical implementation plan. Gateway authentication and mandatory turn recall now follow `2026-10-26-gateway-recall.md` and the current design; older token and disable/auto-false passthrough assumptions below are superseded.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the three Python SDK entry points in the approved design with durable incremental Session reporting and native OpenAI behavior preserved.

**Architecture:** A private HTTP transport powers async/sync atomic resources. A SQLite state store tracks Session ACKs and turn recovery. The OpenAI proxy delegates dynamically and intercepts supported generation methods; the Agents runner composes those same atoms around the native Runner.

**Tech Stack:** Python 3.10+, `httpx`, `openai`, optional `openai-agents`, `pytest`.

**Spec:** `python/docs/2026-10-26-cosens-python-sdk-design.md` and `/Users/a1111/Desktop/RSI_WORKFLOW_API(1).md` (design overrides older SDK entry points).

## Global Constraints

- Public constructors never accept CoSens `base_url`; the deployed endpoint is private to the transport.
- No write tests against the live service; use local fake HTTP transports.
- Approval mode fails explicitly until `/v1/invocations` exists on the deployment.
- Native OpenAI calls and Agents Runner execute locally exactly once; RSI failures after native completion leave a recoverable `completion_pending` state.
- Only durable ACK advances a Session cursor. No inferred acceptance, automatic publishing, or unauthorised Memory API calls.

## Review Focus

- Unknown OpenAI methods and resource trees remain callable without being mislabeled as RSI capture.
- Raw and streamed response wrappers preserve their native lifecycle and return values.
- Concurrent Session updates and history rewrites cannot skip or duplicate ACKed messages.
- Lost submit responses do not rerun the native model or tools.
- Classification failures in `auto` do not send CoSens requests or advance cursors.

---

### Task 1: Atomic HTTP client and package

**Files:** `python/pyproject.toml`, `python/src/cosens/{__init__,_config,errors,models,http,atoms}.py`, `python/tests/test_atoms.py`.

**Interfaces:** `CoSensClient(token=None, *, state_path=None)`, `client.atoms.{artifact,prepare,get_run,session_events,submit,feedback,improve,get_improvement,resume_improvement,wait_improvement}`, `client.admin.atoms.publish` with separate admin credentials. Internal sync atoms for the transparent sync proxy. Result objects expose response fields as attributes.

- [ ] Write tests for request path/body/auth, required keys, response attributes, errors, admin separation, and no public `base_url`; run and observe RED.
- [ ] Implement private transport and atomic methods against the deployed 1.0 contract; run tests to GREEN.
- [ ] Verify the task suite and commit.

### Task 2: Durable Session cursor and turn state

**Files:** `python/src/cosens/{session_cursor,state}.py`, `python/tests/test_session_cursor.py`.

**Interfaces:** `CursorStore.snapshot(session)`, `CursorStore.pending(...)`, `CursorStore.ack(...)`, `TurnStore.get/put(...)`.

- [ ] Write tests for never sent, partial ACK, repeat Run, same-Run retry, history edit, and restart; observe RED.
- [ ] Implement SQLite persistence, stable message IDs, batch limits, and ACK parsing; run tests to GREEN.
- [ ] Verify the task suite and commit.

### Task 3: Explicit Agent run orchestration

**Files:** `python/src/cosens/{factory,agents_adapter,workflow}.py`, `python/tests/test_agent_runner.py`.

**Interfaces:** `cosens(agent, mode="disable")`, `CoSensAgentRunner.run/run_sync/run_streamed`, `client.cosens.get_turn(turn_id)`.

- [ ] Write tests for atomic order, Session delta, Agent clone, native result, pre/post native failure and resume; observe RED.
- [ ] Implement workflow with the task 1 atoms and task 2 state; run tests to GREEN.
- [ ] Verify the task suite and commit.

### Task 4: Transparent OpenAI client

**Files:** `python/src/cosens/{openai_proxy,decision,responses_adapter}.py`, `python/tests/test_openai_proxy.py`.

**Interfaces:** `cosens(OpenAI|AsyncOpenAI, mode="auto"|"approval"|"disable", decision_model=..., session=...)` with `native`, `cosens.get_turn`, and dynamic resource delegation.

- [ ] Write tests for full delegation, copy/options, classification false/true/failure, sync/async generation, continuation, raw and streamed methods; observe RED.
- [ ] Implement decision and model adapters; ensure unsupported variants are exposed as such without altering native return; run tests to GREEN.
- [ ] Verify the task suite and commit.

### Task 5: Documentation, compatibility and complete verification

**Files:** `python/README.md`, `python/tests/test_openai_compat.py`, relevant source files.

- [ ] Write compatibility tests against the installed official OpenAI SDK public resource tree, Agent API and private endpoint boundary; observe RED for gaps.
- [ ] Close gaps, document supported versions and runtime limitations; run tests to GREEN.
- [ ] Run the full test suite, package build, inspect diff, and commit.

