"""Native Agents SDK execution wrapped in atomic CoSens calls."""

import asyncio
import hashlib
import json
import uuid

from .atoms import CoSensClient
from .errors import RSIRecoveryRequired
from .session_cursor import CursorStore
from .state import TurnStore


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


class _StreamResult:
    def __init__(self, owner, native_stream, state, logical_key, session):
        self._owner = owner
        self._native = native_stream
        self._state = state
        self._logical_key = logical_key
        self._session = session

    def __getattr__(self, name):
        return getattr(self._native, name)

    async def stream_events(self):
        try:
            async for event in self._native.stream_events():
                yield event
        except BaseException as exc:
            self._state["state"] = "native_failed"
            self._state["last_error"] = str(exc)
            self._owner._save(self._logical_key, self._state)
            raise
        if not self._native.is_complete or getattr(self._native, "run_loop_exception", None):
            self._state["state"] = "incomplete"
            self._owner._save(self._logical_key, self._state)
            return
        self._state["state"] = "native_completed"
        self._state["result"] = {"summary": str(self._native.final_output), "outcome": "completed"}
        self._owner._save(self._logical_key, self._state)
        try:
            await self._owner._send_pending(self._state["run_id"], self._session)
            submitted = await self._owner._client.atoms.submit(self._state["run_id"], data=self._state["result"])
            if submitted.status != "completed":
                raise RSIRecoveryRequired("CoSens submit did not confirm completion")
            self._state["state"] = "completed"
        except Exception as exc:
            self._state["state"] = "completion_pending"
            self._state["last_error"] = str(exc)
        self._owner._save(self._logical_key, self._state)

    def cancel(self):
        self._state["state"] = "incomplete"
        self._owner._save(self._logical_key, self._state)
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

    async def run(self, agent, user_input, *, session=None, **native_options):
        session = session if session is not None else self.cosens.session
        objective = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False, default=str)
        logical_key = "logical:" + _stable([getattr(session, "session_id", None), agent.name, objective])
        previous = self._turns.get(logical_key)
        if previous and previous.get("state") in {"native_running", "native_completed", "completion_pending"}:
            raise RSIRecoveryRequired(f"Turn {previous['turn_id']} may have executed; reconcile without rerunning Agent")
        turn_id = previous["turn_id"] if previous and previous.get("state") == "pre_execution_pending" else str(uuid.uuid4())
        self.cosens.last_turn_id = turn_id
        state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "run_id": previous.get("run_id") if previous else None}
        self._save(logical_key, state)
        try:
            if state["run_id"]:
                prepared = {"run_id": state["run_id"], "recipe": {}, "context": {}}
            else:
                prepared = await self._client.atoms.prepare(objective=objective, key=turn_id)
                if prepared.status == "processing":
                    raise RSIRecoveryRequired("Run preparation is still processing; retry this turn")
                state["run_id"] = prepared.run_id
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
        state["state"] = "native_completed"
        state["result"] = {"summary": str(native_result.final_output), "outcome": "completed"}
        self._save(logical_key, state)
        try:
            await self._send_pending(state["run_id"], session)
            submitted = await self._client.atoms.submit(state["run_id"], data=state["result"])
            if submitted.status != "completed":
                raise RSIRecoveryRequired("CoSens submit did not confirm completion")
            state["state"] = "completed"
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
        self._save(logical_key, state)
        return native_result

    def _save(self, logical_key, state):
        self._turns.put(state["turn_id"], state)
        self._turns.put(logical_key, state)

    def run_sync(self, agent, user_input, *, session=None, **native_options):
        return asyncio.run(self.run(agent, user_input, session=session, **native_options))

    async def run_streamed(self, agent, user_input, *, session=None, **native_options):
        session = session if session is not None else self.cosens.session
        objective = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False, default=str)
        logical_key = "logical:" + _stable([getattr(session, "session_id", None), agent.name, objective])
        previous = self._turns.get(logical_key)
        if previous and previous.get("state") in {"native_running", "native_completed", "completion_pending"}:
            raise RSIRecoveryRequired(f"Turn {previous['turn_id']} may have executed; reconcile without rerunning Agent")
        turn_id = previous["turn_id"] if previous and previous.get("state") == "pre_execution_pending" else str(uuid.uuid4())
        self.cosens.last_turn_id = turn_id
        state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "run_id": previous.get("run_id") if previous else None}
        self._save(logical_key, state)
        try:
            if state["run_id"]:
                prepared = {"run_id": state["run_id"], "recipe": {}, "context": {}}
            else:
                prepared = await self._client.atoms.prepare(objective=objective, key=turn_id)
                if prepared.status == "processing":
                    raise RSIRecoveryRequired("Run preparation is still processing; retry this turn")
                state["run_id"] = prepared.run_id
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
        return _StreamResult(self, stream, state, logical_key, session)

    async def aclose(self):
        await self._client.aclose()
        self._cursor.close()
        self._turns.close()
