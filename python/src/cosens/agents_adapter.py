"""Native Agents SDK execution wrapped in atomic CoSens calls."""

import asyncio
import hashlib
import json
import threading
import uuid

from .atoms import CoSensClient
from .errors import RSIRecoveryRequired
from .session_cursor import CursorStore
from .state import TurnStore
from .workflow import prepare_ready_async


def _stable(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class _Status:
    def __init__(self, owner, mode, session):
        self._owner = owner
        self.mode = mode
        self.session = session
        self.last_turn_id = None

    def get_turn(self, turn_id):
        return self._owner._turns.get(turn_id)

    async def reconcile(self, turn_id, *, session=None):
        return await self._owner.reconcile(turn_id, session=session)


class _StreamResult:
    def __init__(self, owner, native_stream, state, logical_key, session, lock):
        self._owner = owner
        self._native = native_stream
        self._state = state
        self._logical_key = logical_key
        self._session = session
        self._lock = lock

    def _release_lock(self):
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def __getattr__(self, name):
        return getattr(self._native, name)

    async def stream_events(self):
        try:
            async for event in self._native.stream_events():
                yield event
            if not self._native.is_complete or getattr(self._native, "run_loop_exception", None):
                self._state["state"] = "incomplete"
                self._owner._save(self._logical_key, self._state)
                return
            if getattr(self._native, "interruptions", None):
                self._owner._save_interrupted(self._logical_key, self._state, self._native)
                return
            self._state["state"] = "native_completed"
            self._state["result"] = self._owner._result_data(self._native.final_output)
            self._state["output_files"] = self._owner._output_files(self._native.final_output)
            self._owner._save(self._logical_key, self._state)
            try:
                await self._owner._send_pending(self._state["run_id"], self._session)
                await self._owner._upload_outputs(self._logical_key, self._state)
                submitted = await self._owner._client.atoms.submit(self._state["run_id"], data=self._state["result"])
                if submitted.status != "completed":
                    raise RSIRecoveryRequired("CoSens submit did not confirm completion")
                self._state["state"] = "completed"
            except Exception as exc:
                self._state["state"] = "completion_pending"
                self._state["last_error"] = str(exc)
            self._owner._save(self._logical_key, self._state)
        except GeneratorExit:
            self._state["state"] = "incomplete"
            self._owner._save(self._logical_key, self._state)
            raise
        except BaseException as exc:
            self._state["state"] = "native_failed"
            self._state["last_error"] = str(exc)
            self._owner._save(self._logical_key, self._state)
            raise
        finally:
            self._release_lock()

    def cancel(self):
        self._state["state"] = "incomplete"
        self._owner._save(self._logical_key, self._state)
        self._release_lock()
        return self._native.cancel()


class CoSensAgentRunner:
    def __init__(self, agent, *, mode, session, token, state_path, http_transport, runner):
        self.native = agent
        self._client = CoSensClient(token=token, state_path=state_path, _http_transport=http_transport)
        self._cursor = CursorStore(state_path, principal=self._client._transport.token or "anonymous")
        self._turns = TurnStore(state_path)
        if runner is None:
            try:
                from agents import Runner
            except ImportError as exc:
                raise ImportError("Install cosens-sdk[agents] for Agent support") from exc
            runner = Runner
        self._runner = runner
        self.cosens = _Status(self, mode, session)
        self._session_locks = {}
        self._session_locks_guard = threading.Lock()

    def _session_lock(self, session):
        loop = asyncio.get_running_loop()
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None) or id(session)
        key = (id(loop), str(session_id))
        with self._session_locks_guard:
            return self._session_locks.setdefault(key, asyncio.Lock())

    async def _send_pending(self, run_id, session):
        if session is None:
            return
        snapshot = await self._cursor.snapshot(session)
        recovery = self._cursor.recover_batch(snapshot)
        if recovery and recovery.run_id != run_id:
            raise RSIRecoveryRequired(f"Session has unsent events for Run {recovery.run_id}")
        while True:
            events = recovery.events if recovery else self._cursor.pending(snapshot)
            if not events:
                break
            self._cursor.begin_batch(run_id, snapshot, events)
            receipt = await self._client.atoms.session_events(run_id, events)
            if self._cursor.ack(snapshot, receipt) == 0:
                raise RSIRecoveryRequired("CoSens did not durably acknowledge the first pending event")
            recovery = self._cursor.recover_batch(snapshot)

    @staticmethod
    def _output_files(final_output):
        if not isinstance(final_output, dict):
            return []
        files = final_output.get("files") or []
        if not isinstance(files, list):
            raise TypeError("final_output.files must be a list of text artifacts")
        return files

    @staticmethod
    def _result_data(final_output):
        if isinstance(final_output, dict):
            data = {key: value for key, value in final_output.items() if key != "files"}
            data["summary"] = str(data.get("summary", ""))
        else:
            data = {"summary": str(final_output)}
        data["outcome"] = "completed"
        return data

    async def _upload_inputs(self, logical_key, state):
        materials = state.get("input_artifacts") or []
        refs = state.setdefault("input_refs", [])
        if state.get("input_upload_inflight") is not None:
            raise RSIRecoveryRequired("Initial Artifact upload receipt is unknown; reconcile before retrying")
        for material in materials[len(refs):]:
            if not isinstance(material, dict) or not isinstance(material.get("name"), str) or not isinstance(material.get("content"), str):
                raise TypeError("input_artifacts must contain name/content text objects")
            state["input_upload_inflight"] = len(refs)
            self._save(logical_key, state)
            receipt = await self._client.atoms.artifact(name=material["name"], content=material["content"])
            refs.append({"artifact_id": receipt.artifact_id, "content_hash": receipt.content_hash})
            state["input_upload_inflight"] = None
            self._save(logical_key, state)

    async def _upload_outputs(self, logical_key, state):
        materials = state.get("output_files") or []
        refs = state.setdefault("output_refs", [])
        if state.get("output_upload_inflight") is not None:
            raise RSIRecoveryRequired("Output Artifact upload receipt is unknown; do not re-upload blindly")
        for material in materials[len(refs):]:
            if not isinstance(material, dict) or not isinstance(material.get("name"), str) or not isinstance(material.get("content"), str):
                raise TypeError("final_output.files must contain name/content text objects")
            state["output_upload_inflight"] = len(refs)
            self._save(logical_key, state)
            receipt = await self._client.atoms.artifact(name=material["name"], content=material["content"])
            refs.append({"artifact_id": receipt.artifact_id, "content_hash": receipt.content_hash})
            state["output_upload_inflight"] = None
            self._save(logical_key, state)
        if refs:
            state["result"]["files"] = refs
            self._save(logical_key, state)

    @staticmethod
    def _with_context(agent, prepared, run):
        frozen = {"recipe": prepared.get("recipe"), "context": prepared.get("context"), "snapshot": run.get("snapshot")}
        if not any(frozen.values()):
            return agent.clone()
        block = "\n\n[CoSens frozen context; source: RSI Run " + str(run.run_id) + "]\n" + json.dumps(frozen, ensure_ascii=False, default=str)
        original = agent.instructions
        if original is None or isinstance(original, str):
            return agent.clone(instructions=(original or "") + block)
        if callable(original):
            if asyncio.iscoroutinefunction(original):
                async def instructions(context, current_agent):
                    return (await original(context, current_agent)) + block
            else:
                def instructions(context, current_agent):
                    return original(context, current_agent) + block
            return agent.clone(instructions=instructions)
        raise TypeError("Agent instructions cannot be safely extended with CoSens context")

    async def run(self, agent, user_input, *, session=None, input_artifacts=None, **native_options):
        active_session = session if session is not None else self.cosens.session
        async with self._session_lock(active_session):
            return await self._run_locked(
                agent, user_input, session=active_session,
                input_artifacts=input_artifacts, **native_options,
            )

    async def _run_locked(self, agent, user_input, *, session=None, input_artifacts=None, **native_options):
        session = session if session is not None else self.cosens.session
        objective = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False, default=str)
        logical_key = "logical:" + _stable([getattr(session, "session_id", None), agent.name, objective])
        previous = self._turns.get(logical_key)
        if previous and previous.get("state") in {"native_running", "native_failed", "native_completed", "completion_pending", "incomplete", "awaiting_native_approval"}:
            raise RSIRecoveryRequired(f"Turn {previous['turn_id']} may have executed; reconcile without rerunning Agent")
        resumable = previous if previous and previous.get("state") == "pre_execution_pending" else None
        turn_id = resumable["turn_id"] if resumable else str(uuid.uuid4())
        self.cosens.last_turn_id = turn_id
        state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "run_id": resumable.get("run_id") if resumable else None, "prepared": resumable.get("prepared") if resumable else None, "input_artifacts": input_artifacts if input_artifacts is not None else resumable.get("input_artifacts", []) if resumable else [], "input_refs": resumable.get("input_refs", []) if resumable else []}
        self._save(logical_key, state)
        try:
            if state["run_id"]:
                prepared = state["prepared"] or {"run_id": state["run_id"], "recipe": {}, "context": {}}
            else:
                await self._upload_inputs(logical_key, state)
                prepared = await prepare_ready_async(
                    self._client.atoms, objective=objective, key=turn_id,
                    input_artifact_ids=[ref["artifact_id"] for ref in state["input_refs"]],
                )
                state["run_id"] = prepared.run_id
                state["prepared"] = dict(prepared)
                self._save(logical_key, state)
            run = await self._client.atoms.get_run(state["run_id"])
            await self._send_pending(state["run_id"], session)
            active_agent = self._with_context(agent, prepared, run)
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(logical_key, state)
            raise
        state["state"] = "native_running"
        self._save(logical_key, state)
        try:
            native_result = await self._runner.run(active_agent, user_input, session=session, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(logical_key, state)
            raise
        if getattr(native_result, "interruptions", None):
            self._save_interrupted(logical_key, state, native_result)
            return native_result
        await self._complete_result(logical_key, state, native_result, session)
        return native_result

    def _save(self, logical_key, state):
        state["logical_key"] = logical_key
        self._turns.put(state["turn_id"], state)
        self._turns.put(logical_key, state)

    def _save_interrupted(self, logical_key, state, native_result):
        state["native_state"] = native_result.to_state().to_json()
        state["interruption_count"] = len(native_result.interruptions)
        state["state"] = "awaiting_native_approval"
        self._save(logical_key, state)

    async def pending_state(self, agent, turn_id):
        state = self._turns.get(turn_id)
        if not state or state.get("state") != "awaiting_native_approval":
            raise RSIRecoveryRequired("Turn has no pending native tool approval")
        from agents import RunState
        run = await self._client.atoms.get_run(state["run_id"])
        active_agent = self._with_context(agent, state["prepared"], run)
        return await RunState.from_json(active_agent, state["native_state"])

    async def resume(self, agent, turn_id, native_state, *, session=None, **native_options):
        active_session = session if session is not None else self.cosens.session
        async with self._session_lock(active_session):
            state = self._turns.get(turn_id)
            if not state or state.get("state") != "awaiting_native_approval":
                raise RSIRecoveryRequired("Turn is not awaiting native tool approval")
            logical_key = state["logical_key"]
            run = await self._client.atoms.get_run(state["run_id"])
            active_agent = self._with_context(agent, state["prepared"], run)
            state["state"] = "native_running"
            self._save(logical_key, state)
            try:
                native_result = await self._runner.run(active_agent, native_state, session=active_session, **native_options)
            except Exception as exc:
                state["state"] = "native_failed"
                state["last_error"] = str(exc)
                self._save(logical_key, state)
                raise
            if getattr(native_result, "interruptions", None):
                self._save_interrupted(logical_key, state, native_result)
                return native_result
            await self._complete_result(logical_key, state, native_result, active_session)
            return native_result

    async def _complete_result(self, logical_key, state, native_result, session):
        state["state"] = "native_completed"
        state["result"] = self._result_data(native_result.final_output)
        state["output_files"] = self._output_files(native_result.final_output)
        self._save(logical_key, state)
        try:
            await self._send_pending(state["run_id"], session)
            await self._upload_outputs(logical_key, state)
            submitted = await self._client.atoms.submit(state["run_id"], data=state["result"])
            if submitted.status != "completed":
                raise RSIRecoveryRequired("CoSens submit did not confirm completion")
            state["state"] = "completed"
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
        self._save(logical_key, state)

    async def reconcile(self, turn_id, *, session=None):
        state = self._turns.get(turn_id)
        if not state or state.get("state") not in {"native_completed", "completion_pending"}:
            raise RSIRecoveryRequired("Turn has no completed native result available for reconciliation")
        session = session if session is not None else self.cosens.session
        try:
            await self._send_pending(state["run_id"], session)
            await self._upload_outputs(state.get("logical_key", "logical:" + turn_id), state)
            remote = await self._client.atoms.get_run(state["run_id"])
            if remote.status == "completed":
                if remote.get("result") != state.get("result"):
                    raise RSIRecoveryRequired("Remote completed Run result differs from locally saved result")
            elif remote.status == "ready":
                receipt = await self._client.atoms.submit(state["run_id"], data=state["result"])
                if receipt.status != "completed":
                    raise RSIRecoveryRequired("CoSens did not confirm the recovered submit")
            else:
                raise RSIRecoveryRequired(f"Run {state['run_id']} is {remote.status}; cannot safely submit")
            state["state"] = "completed"
            state.pop("last_error", None)
            self._save(state.get("logical_key", "logical:" + turn_id), state)
            return state
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state.get("logical_key", "logical:" + turn_id), state)
            raise

    def run_sync(self, agent, user_input, *, session=None, **native_options):
        return asyncio.run(self.run(agent, user_input, session=session, **native_options))

    async def run_streamed(self, agent, user_input, *, session=None, input_artifacts=None, **native_options):
        active_session = session if session is not None else self.cosens.session
        lock = self._session_lock(active_session)
        await lock.acquire()
        try:
            return await self._run_streamed_locked(
                agent, user_input, session=active_session, input_artifacts=input_artifacts,
                _lock=lock, **native_options,
            )
        except BaseException:
            lock.release()
            raise

    async def _run_streamed_locked(self, agent, user_input, *, session=None, input_artifacts=None, _lock, **native_options):
        session = session if session is not None else self.cosens.session
        objective = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False, default=str)
        logical_key = "logical:" + _stable([getattr(session, "session_id", None), agent.name, objective])
        previous = self._turns.get(logical_key)
        if previous and previous.get("state") in {"native_running", "native_failed", "native_completed", "completion_pending", "incomplete", "awaiting_native_approval"}:
            raise RSIRecoveryRequired(f"Turn {previous['turn_id']} may have executed; reconcile without rerunning Agent")
        resumable = previous if previous and previous.get("state") == "pre_execution_pending" else None
        turn_id = resumable["turn_id"] if resumable else str(uuid.uuid4())
        self.cosens.last_turn_id = turn_id
        state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "run_id": resumable.get("run_id") if resumable else None, "prepared": resumable.get("prepared") if resumable else None, "input_artifacts": input_artifacts if input_artifacts is not None else resumable.get("input_artifacts", []) if resumable else [], "input_refs": resumable.get("input_refs", []) if resumable else []}
        self._save(logical_key, state)
        try:
            if state["run_id"]:
                prepared = state["prepared"] or {"run_id": state["run_id"], "recipe": {}, "context": {}}
            else:
                await self._upload_inputs(logical_key, state)
                prepared = await prepare_ready_async(
                    self._client.atoms, objective=objective, key=turn_id,
                    input_artifact_ids=[ref["artifact_id"] for ref in state["input_refs"]],
                )
                state["run_id"] = prepared.run_id
                state["prepared"] = dict(prepared)
                self._save(logical_key, state)
            run = await self._client.atoms.get_run(state["run_id"])
            await self._send_pending(state["run_id"], session)
            active_agent = self._with_context(agent, prepared, run)
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(logical_key, state)
            raise
        state["state"] = "native_running"
        self._save(logical_key, state)
        try:
            stream = self._runner.run_streamed(active_agent, user_input, session=session, **native_options)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(logical_key, state)
            raise
        return _StreamResult(self, stream, state, logical_key, session, _lock)

    async def aclose(self):
        await self._client.aclose()
        self._cursor.close()
        self._turns.close()
