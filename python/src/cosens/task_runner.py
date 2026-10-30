"""Opt-in native Runner wrapper for the task workflow protocol."""

import asyncio
import copy
import hashlib
import inspect
import json

from .atoms import CoSensClient
from .errors import RSIApprovalRequired, RSIApprovalUnavailable, RSIRecoveryRequired
from .state import TurnStore
from .workflow_start import _derived_key


def _text_output(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _observed_native_items(result):
    """Record bounded items actually returned by Agents, without claiming a complete trace."""
    items = getattr(result, "new_items", None)
    if not isinstance(items, (list, tuple)):
        return {"events": [], "trace_coverage": "unknown"}
    events = []
    for index, item in enumerate(items[:200]):
        kind = getattr(item, "type", None)
        if not isinstance(kind, str) or not kind:
            continue
        payload = {"item_type": kind}
        raw = getattr(item, "raw_item", None)
        try:
            if hasattr(raw, "model_dump"):
                raw = raw.model_dump(exclude_unset=True)
            encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) <= 4096:
                payload["raw_item"] = json.loads(encoded)
        except Exception:
            pass
        events.append({"id": f"native-item-{index + 1}", "kind": kind, "payload": payload})
    return {"events": events, "trace_coverage": "partial" if events else "unknown"}


def _agent_with_request(agent, request):
    block = "\n\n[CoSens frozen task execution request]\n" + json.dumps(request, ensure_ascii=False, default=str)
    original = getattr(agent, "instructions", None)
    if not hasattr(agent, "clone"):
        raise TypeError("Agent must support clone to receive the frozen execution request")
    if original is None or isinstance(original, str):
        return agent.clone(instructions=(original or "") + block)
    if callable(original):
        if inspect.iscoroutinefunction(original):
            async def instructions(context, current_agent):
                return str(await original(context, current_agent)) + block
        else:
            def instructions(context, current_agent):
                return str(original(context, current_agent)) + block
        return agent.clone(instructions=instructions)
    raise TypeError("Agent instructions cannot safely receive a frozen execution request")


class _WorkflowStatus:
    def __init__(self, owner):
        self._owner = owner
        self.mode = owner.mode
        self.session = None
        self.last_turn_id = None
        self.atoms = owner._client.atoms

    def get_turn(self, turn_id):
        return self._owner._store.get(turn_id)

    async def reconcile(self, turn_id, *, session=None):
        return await self._owner.reconcile(turn_id, session=session)

    def reconcile_sync(self, turn_id, *, session=None):
        return self._owner.reconcile_sync(turn_id, session=session)


class _TaskStream:
    def __init__(self, owner, turn_id, native):
        self._owner, self._turn_id, self._native = owner, turn_id, native

    def __getattr__(self, name):
        return getattr(self._native, name)

    def cancel(self, *args, **kwargs):
        state = self._owner._store.get(self._turn_id)
        if state["state"] == "native_running":
            state["state"] = "incomplete"
            self._owner._save(state)
        return self._native.cancel(*args, **kwargs)

    async def stream_events(self):
        try:
            async for event in self._native.stream_events():
                yield event
            state = self._owner._store.get(self._turn_id)
            if state["state"] != "native_running":
                return
            if not getattr(self._native, "is_complete", False) or getattr(self._native, "run_loop_exception", None):
                state["state"] = "incomplete"
                self._owner._save(state)
                return
            if getattr(self._native, "interruptions", None):
                self._owner._save_interrupted(state, self._native)
                return
            await self._owner._complete(state, self._native)
        except BaseException:
            state = self._owner._store.get(self._turn_id)
            if state["state"] == "native_running":
                state["state"] = "incomplete"
                self._owner._save(state)
            raise


class TaskWorkflowRunner:
    """Wrap an Agents Runner class/instance without changing existing Run wrappers."""

    def __init__(self, native_runner, *, mode="disable", api_key=None, state_path=None,
                 result_adapter=None, trace_adapter=None, on_rsi_event=None, _http_transport=None):
        if mode not in {"auto", "approval", "disable"}:
            raise ValueError("mode must be auto, approval or disable")
        self.native = native_runner
        self.mode = mode
        self._client = CoSensClient(api_key=api_key, state_path=state_path, _http_transport=_http_transport)
        self._store = TurnStore(state_path, principal=self._client._transport.api_key, service_id=self._client._service.identity)
        self._result_adapter = result_adapter
        self._trace_adapter = trace_adapter
        self._on_rsi_event = on_rsi_event
        self.cosens = _WorkflowStatus(self)

    def _save(self, state):
        self._store.put(state["turn_id"], state)

    def _claim_native(self, state, expected):
        state["state"] = "native_running"
        if not self._store.claim_state(state["turn_id"], expected_state=expected, value=state):
            raise RSIRecoveryRequired("Workflow turn was claimed by another runner; reconcile before retrying")

    def _save_pending(self, state):
        if not self._store.update_pending(state["turn_id"], state):
            raise RSIRecoveryRequired("Workflow turn changed during preparation; reconcile before retrying")

    @staticmethod
    def _session_identity(session):
        if session is None:
            return None, "0"
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
        if session_id is None:
            raise ValueError("Workflow Session requires a stable session_id or id")
        incarnation = getattr(session, "session_incarnation", None) or getattr(session, "incarnation", "0")
        return str(session_id), str(incarnation)

    @staticmethod
    def _input_hash(agent, user_input):
        try:
            encoded = json.dumps({"agent_name": getattr(agent, "name", None), "input": user_input},
                                 sort_keys=True, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("Workflow input must be JSON serializable for stable turn recovery") from exc
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _require_session(self, state, session):
        session_id, incarnation = self._session_identity(session)
        if state.get("session_id") != session_id or state.get("session_incarnation") != incarnation:
            raise RSIRecoveryRequired("Workflow turn belongs to a different Session incarnation")

    async def _emit(self, name, state):
        if self._on_rsi_event:
            try:
                value = self._on_rsi_event(name, copy.deepcopy(state))
                if inspect.isawaitable(value):
                    await value
            except Exception as exc:
                state["event_callback_error"] = str(exc)
                if state.get("state") == "pre_execution_pending":
                    try:
                        self._save_pending(state)
                    except RSIRecoveryRequired:
                        pass
                else:
                    self._save(state)

    def _emit_sync(self, name, state):
        if self._on_rsi_event:
            try:
                value = self._on_rsi_event(name, copy.deepcopy(state))
                if inspect.isawaitable(value):
                    if inspect.iscoroutine(value):
                        value.close()
                    raise TypeError("synchronous Runner requires a synchronous on_rsi_event callback")
            except Exception as exc:
                state["event_callback_error"] = str(exc)
                if state.get("state") == "pre_execution_pending":
                    try:
                        self._save_pending(state)
                    except RSIRecoveryRequired:
                        pass
                else:
                    self._save(state)

    def _save_interrupted(self, state, native_result):
        state["state"] = "awaiting_native_approval"
        state["native_state"] = native_result.to_state().to_json()
        self._save(state)

    def _pending_state(self, rsi, *, agent, user_input, session):
        if not isinstance(rsi, dict) or not isinstance(rsi.get("turn_id"), str) or not rsi["turn_id"].strip():
            raise ValueError("rsi.turn_id is required for a recoverable workflow turn")
        if not isinstance(rsi.get("task"), dict):
            raise ValueError("rsi.task is required for a workflow turn")
        session_id, session_incarnation = self._session_identity(session)
        input_hash = self._input_hash(agent, user_input)
        turn_id = rsi["turn_id"]
        state = self._store.get(turn_id)
        if state and state.get("state") != "pre_execution_pending":
            raise RSIRecoveryRequired(f"Workflow turn {turn_id} may have executed; reconcile without rerunning Agent")
        if state and state.get("task_spec") != rsi.get("task"):
            raise RSIRecoveryRequired("Workflow task changed while recovering the same turn")
        if state and (state.get("session_id") != session_id or state.get("session_incarnation") != session_incarnation or state.get("input_hash") != input_hash):
            raise RSIRecoveryRequired("Workflow turn input or Session identity changed during recovery")
        if not state:
            state = {"turn_id": turn_id, "task_spec": rsi.get("task"), "state": "pre_execution_pending", "mode": self.mode,
                     "session_id": session_id, "session_incarnation": session_incarnation, "input_hash": input_hash}
            if not self._store.insert_if_absent(turn_id, state):
                state = self._store.get(turn_id)
                if (state.get("state") != "pre_execution_pending" or state.get("task_spec") != rsi.get("task") or
                        state.get("session_id") != session_id or state.get("session_incarnation") != session_incarnation or state.get("input_hash") != input_hash):
                    raise RSIRecoveryRequired("Workflow turn was already claimed or configured differently")
        if state.get("mode") != self.mode:
            raise RSIRecoveryRequired("Workflow turn originated in a different mode")
        self.cosens.last_turn_id = turn_id
        return state

    async def _prepare(self, rsi, *, agent, user_input, session):
        state = self._pending_state(rsi, agent=agent, user_input=user_input, session=session)
        turn_id = state["turn_id"]
        if not state.get("execution_id"):
            started = await self._client.start({"task_id": state["task_id"]} if state.get("task_id") else {"task": state["task_spec"]}, phase="prepare", mode=self.mode,
                                               request_key=turn_id, authorize=rsi.get("authorize"))
            if started.get("task_id") or started.get("task", {}).get("task_id"):
                state["task_id"] = started.get("task_id") or started["task"]["task_id"]
                self._save_pending(state)
            if started.get("status") != "awaiting_execution" or not started.get("execution_id") or not started.get("request_sha256") or not isinstance(started.get("execution_request"), dict):
                raise RSIRecoveryRequired(f"Workflow task is {started.get('status')}; provide context before executing")
            state.update(task_id=started["task_id"], execution_id=started["execution_id"],
                         request_sha256=started["request_sha256"], request=started["execution_request"])
            self._save_pending(state)
        await self._emit("awaiting_execution", state)
        return state

    def _prepare_sync(self, rsi, *, agent, user_input, session):
        state = self._pending_state(rsi, agent=agent, user_input=user_input, session=session)
        if not state.get("execution_id"):
            if self.mode == "approval":
                authorize = rsi.get("authorize")
                if authorize is None:
                    raise RSIApprovalUnavailable("approval mode requires a host authorization callback")
                snapshot = {"task_id": state["task_id"]} if state.get("task_id") else {"task": state["task_spec"]}
                allowed = authorize("prepare", snapshot)
                if inspect.isawaitable(allowed):
                    if inspect.iscoroutine(allowed):
                        allowed.close()
                    raise TypeError("synchronous Runner requires a synchronous authorization callback")
                if allowed is not True:
                    raise RSIApprovalRequired("host authorization was not granted")
            atoms = self._client.sync_atoms
            if state.get("task_id"):
                task = atoms.get_task(state["task_id"])
            else:
                data = state["task_spec"]
                task = atoms.create_task(source=data["source"], objective=data.get("objective"),
                                         artifacts=data.get("artifacts"), self_contained=data.get("self_contained"),
                                         key=_derived_key(state["turn_id"], "task"))
            task_id = task.get("task_id")
            if task_id:
                state["task_id"] = task_id
                self._save_pending(state)
            if task.get("status") != "ready" or not task_id:
                raise RSIRecoveryRequired(f"Workflow task is {task.get('status')}; provide context before executing")
            execution = atoms.start_execution(task_id, key=_derived_key(state["turn_id"], "execution"))
            if (execution.get("status") != "awaiting_execution" or not execution.get("execution_id") or
                    not execution.get("request_sha256") or not isinstance(execution.get("request"), dict)):
                raise RSIRecoveryRequired(f"Workflow execution is {execution.get('status')}; reconcile before executing")
            state.update(execution_id=execution["execution_id"], request_sha256=execution["request_sha256"], request=execution["request"])
            self._save_pending(state)
        self._emit_sync("awaiting_execution", state)
        return state

    async def _materials(self, final_output):
        if self._result_adapter:
            value = self._result_adapter(final_output)
            materials = await value if inspect.isawaitable(value) else value
        elif isinstance(final_output, dict) and final_output.get("files") is not None:
            materials = final_output["files"]
        else:
            if final_output is None:
                raise ValueError("native result has no output; supply result_adapter before execution")
            materials = [{"name": "result.txt", "content": _text_output(final_output)}]
        if not isinstance(materials, list) or not materials:
            raise ValueError("completed workflow output requires at least one text artifact")
        for material in materials:
            if not isinstance(material, dict) or not isinstance(material.get("name"), str) or not isinstance(material.get("content"), str):
                raise TypeError("result_adapter must return text artifact objects with name/content")
        return materials

    async def _send_result(self, state):
        if state.get("upload_inflight") is not None:
            raise RSIRecoveryRequired("Artifact upload receipt is unknown; reconcile with the service before retrying")
        materials = state["materials"]
        refs = state.setdefault("output_refs", [])
        for material in materials[len(refs):]:
            state["upload_inflight"] = len(refs)
            self._save(state)
            receipt = await self._client.atoms.artifact(name=material["name"], content=material["content"])
            if not isinstance(receipt.get("artifact_id"), str) or not receipt["artifact_id"] or not isinstance(receipt.get("content_hash"), str) or not receipt["content_hash"]:
                raise RSIRecoveryRequired("Artifact receipt lacks ID or server content hash")
            refs.append({"artifact_id": receipt["artifact_id"], "content_hash": receipt["content_hash"]})
            state["upload_inflight"] = None
            self._save(state)
        if not state.get("result_body"):
            state["result_body"] = {"request_sha256": state["request_sha256"], "outcome": "completed",
                                    "artifacts": [{"artifact_id": ref["artifact_id"], "role": "submitted_output"} for ref in refs],
                                    "events": state.get("events", []), "trace_coverage": state.get("trace_coverage", "unknown")}
            self._save(state)
        receipt = await self._client.atoms.submit_execution(state["execution_id"], **state["result_body"])
        if receipt.get("status") in {"failed", "cancelled"} or not isinstance(receipt.get("revision_id"), str) or not receipt["revision_id"]:
            raise RSIRecoveryRequired("Workflow result was not confirmed as completed")
        state["revision_id"] = receipt.get("revision_id")
        state["state"] = "completed"
        state.pop("last_error", None)
        self._save(state)
        await self._emit("completed", state)

    async def _complete(self, state, native_result):
        state["state"] = "native_completed"
        self._save(state)
        try:
            state["materials"] = await self._materials(native_result.final_output)
            if self._trace_adapter:
                observed = self._trace_adapter(native_result)
                observed = await observed if inspect.isawaitable(observed) else observed
                if not isinstance(observed, dict):
                    raise TypeError("trace_adapter must return events and trace_coverage")
                state["events"] = observed.get("events", [])
                state["trace_coverage"] = observed.get("trace_coverage", "unknown")
            else:
                state.update(_observed_native_items(native_result))
            self._save(state)
            await self._send_result(state)
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)

    def _send_result_sync(self, state):
        if state.get("upload_inflight") is not None:
            raise RSIRecoveryRequired("Artifact upload receipt is unknown; reconcile with the service before retrying")
        refs = state.setdefault("output_refs", [])
        for material in state["materials"][len(refs):]:
            state["upload_inflight"] = len(refs)
            self._save(state)
            receipt = self._client.sync_atoms.artifact(name=material["name"], content=material["content"])
            if not isinstance(receipt.get("artifact_id"), str) or not receipt["artifact_id"] or not isinstance(receipt.get("content_hash"), str) or not receipt["content_hash"]:
                raise RSIRecoveryRequired("Artifact receipt lacks ID or server content hash")
            refs.append({"artifact_id": receipt["artifact_id"], "content_hash": receipt["content_hash"]})
            state["upload_inflight"] = None
            self._save(state)
        if not state.get("result_body"):
            state["result_body"] = {"request_sha256": state["request_sha256"], "outcome": "completed",
                                    "artifacts": [{"artifact_id": ref["artifact_id"], "role": "submitted_output"} for ref in refs],
                                    "events": state.get("events", []), "trace_coverage": state.get("trace_coverage", "unknown")}
            self._save(state)
        receipt = self._client.sync_atoms.submit_execution(state["execution_id"], **state["result_body"])
        if receipt.get("status") in {"failed", "cancelled"} or not isinstance(receipt.get("revision_id"), str) or not receipt["revision_id"]:
            raise RSIRecoveryRequired("Workflow result was not confirmed as completed")
        state["revision_id"] = receipt["revision_id"]
        state["state"] = "completed"
        state.pop("last_error", None)
        self._save(state)
        self._emit_sync("completed", state)

    def _complete_sync(self, state, native_result):
        state["state"] = "native_completed"
        self._save(state)
        try:
            state["materials"] = asyncio.run(self._materials(native_result.final_output))
            if self._trace_adapter:
                observed = self._trace_adapter(native_result)
                if inspect.isawaitable(observed):
                    observed = asyncio.run(observed)
                if not isinstance(observed, dict):
                    raise TypeError("trace_adapter must return events and trace_coverage")
                state["events"] = observed.get("events", [])
                state["trace_coverage"] = observed.get("trace_coverage", "unknown")
            else:
                state.update(_observed_native_items(native_result))
            self._save(state)
            self._send_result_sync(state)
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)

    async def run(self, agent, user_input, *, rsi=None, **native_options):
        if self.mode == "disable" or rsi is None:
            return await self.native.run(agent, user_input, **native_options)
        state = await self._prepare(rsi, agent=agent, user_input=user_input, session=native_options.get("session"))
        active_agent = _agent_with_request(agent, state["request"])
        self._claim_native(state, "pre_execution_pending")
        try:
            native_result = await self.native.run(active_agent, user_input, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        if getattr(native_result, "interruptions", None):
            self._save_interrupted(state, native_result)
        else:
            await self._complete(state, native_result)
        return native_result

    def run_streamed(self, agent, user_input, *, rsi=None, **native_options):
        if self.mode == "disable" or rsi is None:
            return self.native.run_streamed(agent, user_input, **native_options)
        state = self._prepare_sync(rsi, agent=agent, user_input=user_input, session=native_options.get("session"))
        active_agent = _agent_with_request(agent, state["request"])
        self._claim_native(state, "pre_execution_pending")
        try:
            native = self.native.run_streamed(active_agent, user_input, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        return _TaskStream(self, state["turn_id"], native)

    def run_sync(self, agent, user_input, *, rsi=None, **native_options):
        if self.mode == "disable" or rsi is None:
            return self.native.run_sync(agent, user_input, **native_options)
        state = self._prepare_sync(rsi, agent=agent, user_input=user_input, session=native_options.get("session"))
        active_agent = _agent_with_request(agent, state["request"])
        self._claim_native(state, "pre_execution_pending")
        try:
            native_result = self.native.run_sync(active_agent, user_input, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        if getattr(native_result, "interruptions", None):
            self._save_interrupted(state, native_result)
        else:
            self._complete_sync(state, native_result)
        return native_result

    async def reconcile(self, turn_id, *, session=None):
        state = self._store.get(turn_id)
        if not state or state.get("state") not in {"native_completed", "completion_pending"}:
            raise RSIRecoveryRequired("Turn has no completed native result available for reconciliation")
        self._require_session(state, session)
        if not state.get("materials"):
            raise RSIRecoveryRequired("Saved native result has no recoverable materials")
        try:
            await self._send_result(state)
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        return state

    def reconcile_sync(self, turn_id, *, session=None):
        state = self._store.get(turn_id)
        if not state or state.get("state") not in {"native_completed", "completion_pending"}:
            raise RSIRecoveryRequired("Turn has no completed native result available for reconciliation")
        self._require_session(state, session)
        if not state.get("materials"):
            raise RSIRecoveryRequired("Saved native result has no recoverable materials")
        try:
            self._send_result_sync(state)
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        return state

    async def pending_state(self, agent, turn_id, *, session=None):
        state = self._store.get(turn_id)
        if not state or state.get("state") != "awaiting_native_approval":
            raise RSIRecoveryRequired("Turn has no pending native approval")
        self._require_session(state, session)
        from agents import RunState
        return await RunState.from_json(_agent_with_request(agent, state["request"]), state["native_state"])

    async def resume(self, agent, turn_id, native_state, **native_options):
        state = self._store.get(turn_id)
        if not state or state.get("state") != "awaiting_native_approval":
            raise RSIRecoveryRequired("Turn is not awaiting native approval")
        self._require_session(state, native_options.get("session"))
        self._claim_native(state, "awaiting_native_approval")
        try:
            native_result = await self.native.run(_agent_with_request(agent, state["request"]), native_state, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        if getattr(native_result, "interruptions", None):
            self._save_interrupted(state, native_result)
        else:
            await self._complete(state, native_result)
        return native_result

    async def aclose(self):
        await self._client.aclose()
        self._store.close()

    def close(self):
        self._client.close()
        self._store.close()
