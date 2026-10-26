"""Dynamic OpenAI resource proxy with narrow RSI generation hooks."""

import asyncio
import contextlib
import contextvars
import functools
import inspect
import json
import uuid

from .atoms import CoSensClient
from .decision import decide_async, decide_sync
from .errors import RSIApprovalUnavailable, RSIDecisionUnavailable, RSIRecoveryRequired
from .responses_adapter import SUPPORTED, base_path, inject, summary, terminal, tool_names, user_text
from .session_cursor import CursorStore
from .state import TurnStore


def _resource(value):
    module = type(value).__module__
    return module.startswith("openai.resources") or module.startswith("openai._response")


class _Status:
    def __init__(self, runtime):
        self._runtime = runtime
        self.mode = runtime.mode
        self.session = runtime.session
        self.last_turn_id = None
        self.rsi_capture = None
        self._scope = contextvars.ContextVar("cosens_scope", default=None)

    def get_turn(self, turn_id):
        return self._runtime.turns.get(turn_id)

    @contextlib.contextmanager
    def scope(self, *, session_id=None, turn_id=None):
        token = self._scope.set((session_id, turn_id))
        try:
            yield self
        finally:
            self._scope.reset(token)


class _RawResponse:
    def __init__(self, native, runtime, state, *, asynchronous):
        self._native = native
        self._runtime = runtime
        self._state = state
        self._asynchronous = asynchronous

    def __getattr__(self, name):
        if name == "parse":
            parse = getattr(self._native, name)
            if inspect.iscoroutinefunction(parse):
                async def async_parse(*args, **kwargs):
                    value = await parse(*args, **kwargs)
                    if self._state["state"] == "native_running":
                        await self._runtime._complete_async(self._state, value)
                    return value
                return async_parse

            def sync_parse(*args, **kwargs):
                value = parse(*args, **kwargs)
                if self._state["state"] == "native_running":
                    self._runtime._complete_sync(self._state, value)
                return value
            return sync_parse
        return getattr(self._native, name)

    def __enter__(self):
        entered = self._native.__enter__()
        return _RawResponse(entered, self._runtime, self._state, asynchronous=False)

    def __exit__(self, exc_type, exc, tb):
        return self._native.__exit__(exc_type, exc, tb)

    async def __aenter__(self):
        entered = await self._native.__aenter__()
        return _RawResponse(entered, self._runtime, self._state, asynchronous=True)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._native.__aexit__(exc_type, exc, tb)


class _GenerationStream:
    def __init__(self, native, runtime, state, *, asynchronous):
        self._native = native
        self._runtime = runtime
        self._state = state
        self._asynchronous = asynchronous
        self._complete_response = None
        self._finished = False
        self._text = []

    def __getattr__(self, name):
        return getattr(self._native, name)

    def _inspect_event(self, event):
        event_type = getattr(event, "type", None)
        if event_type == "response.completed":
            self._complete_response = getattr(event, "response", None)
        for choice in getattr(event, "choices", []) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if content:
                self._text.append(content)
            if getattr(choice, "finish_reason", None) not in {None, "tool_calls", "function_call"}:
                from types import SimpleNamespace
                self._complete_response = SimpleNamespace(
                    id=getattr(event, "id", None),
                    status="completed",
                    choices=[SimpleNamespace(finish_reason=choice.finish_reason, message=SimpleNamespace(content="".join(self._text)))],
                )

    def _incomplete(self):
        if not self._finished and self._state["state"] == "native_running":
            self._state["state"] = "incomplete"
            self._runtime._save(self._state)

    def __iter__(self):
        try:
            for event in self._native:
                self._inspect_event(event)
                yield event
        except BaseException:
            self._incomplete()
            raise
        if self._complete_response is not None:
            self._runtime._complete_sync(self._state, self._complete_response)
            self._finished = True
        else:
            self._incomplete()

    async def __aiter__(self):
        try:
            async for event in self._native:
                self._inspect_event(event)
                yield event
        except BaseException:
            self._incomplete()
            raise
        if self._complete_response is not None:
            await self._runtime._complete_async(self._state, self._complete_response)
            self._finished = True
        else:
            self._incomplete()

    def __enter__(self):
        entered = self._native.__enter__()
        return _GenerationStream(entered, self._runtime, self._state, asynchronous=False)

    def __exit__(self, exc_type, exc, tb):
        self._incomplete()
        return self._native.__exit__(exc_type, exc, tb)

    async def __aenter__(self):
        entered = await self._native.__aenter__()
        return _GenerationStream(entered, self._runtime, self._state, asynchronous=True)

    async def __aexit__(self, exc_type, exc, tb):
        self._incomplete()
        return await self._native.__aexit__(exc_type, exc, tb)


class _DeferredAsyncManager:
    """Preserve native async context-manager syntax while preparing RSI on entry."""

    def __init__(self, native_client, method, path, args, kwargs, runtime):
        self._client = native_client
        self._method = method
        self._path = path
        self._args = args
        self._kwargs = kwargs
        self._runtime = runtime
        self._manager = None

    async def __aenter__(self):
        runtime = self._runtime
        if runtime.mode == "disable" or self._args:
            runtime.status.rsi_capture = "disabled" if runtime.mode == "disable" else "unsupported"
            self._manager = self._method(*self._args, **self._kwargs)
            return await self._manager.__aenter__()
        try:
            decision = await decide_async(
                self._client, runtime.decision_model,
                user_text(self._path, self._kwargs), tool_names(self._kwargs),
            )
        except Exception as exc:
            if runtime.mode == "approval":
                raise RSIDecisionUnavailable(str(exc)) from exc
            runtime.status.rsi_capture = "decision_failed"
            self._manager = self._method(**self._kwargs)
            return await self._manager.__aenter__()
        if not decision["activate"]:
            runtime.status.rsi_capture = "skipped"
            self._manager = self._method(**self._kwargs)
            return await self._manager.__aenter__()
        if runtime.mode == "approval":
            raise RSIApprovalUnavailable("The configured CoSens service has no /v1/invocations approval endpoint")
        if not decision["objective"].strip():
            decision["objective"] = user_text(self._path, self._kwargs)
        state, run = await runtime._prepare_async(decision, self._kwargs)
        state["visible_input"] = user_text(self._path, self._kwargs)
        before = await runtime._snapshot_async()
        state["session_before_count"] = len(before.items) if before else 0
        runtime._save(state)
        updated = inject(self._path, self._kwargs, run)
        state["state"] = "native_running"
        runtime._save(state)
        try:
            self._manager = self._method(**updated)
            entered = await self._manager.__aenter__()
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            runtime._save(state)
            raise
        if "with_streaming_response" in self._path:
            return _RawResponse(entered, runtime, state, asynchronous=True)
        return _GenerationStream(entered, runtime, state, asynchronous=True)

    async def __aexit__(self, exc_type, exc, tb):
        return await self._manager.__aexit__(exc_type, exc, tb)


class _Runtime:
    def __init__(self, *, mode, decision_model, session, token, state_path, http_transport):
        if mode in {"auto", "approval"} and not decision_model:
            raise ValueError("decision_model is required for auto/approval")
        self.mode = mode
        self.decision_model = decision_model
        self.session = session
        self.client = CoSensClient(token=token, state_path=state_path, _http_transport=http_transport)
        self.cursor = CursorStore(state_path, principal=self.client._transport.token or "anonymous")
        self.turns = TurnStore(state_path)
        self.status = _Status(self)

    def _new_state(self, objective, task_family, previous_response_id=None):
        previous = self.turns.get("response:" + str(previous_response_id)) if previous_response_id else None
        if previous and previous.get("state") == "awaiting_continuation":
            state = previous
        else:
            scope = self.status._scope.get()
            turn_id = scope[1] if scope and scope[1] else str(uuid.uuid4())
            state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "task_family": task_family, "run_id": None}
        self.status.last_turn_id = state["turn_id"]
        return state

    def _save(self, state):
        self.turns.put(state["turn_id"], state)

    def _snapshot_sync(self):
        if self.session is None:
            return None
        return asyncio.run(self.cursor.snapshot(self.session))

    async def _snapshot_async(self):
        if self.session is None:
            return None
        return await self.cursor.snapshot(self.session)

    def _send_sync(self, run_id):
        snapshot = self._snapshot_sync()
        if snapshot is None:
            return
        recovery = self.cursor.recover_batch(snapshot)
        if recovery and recovery.run_id != run_id:
            raise RSIRecoveryRequired(f"Session has unsent events for Run {recovery.run_id}")
        while True:
            events = recovery.events if recovery else self.cursor.pending(snapshot)
            if not events:
                return
            self.cursor.begin_batch(run_id, snapshot, events)
            receipt = self.client.sync_atoms.session_events(run_id, events)
            if self.cursor.ack(snapshot, receipt) == 0:
                raise RSIRecoveryRequired("Session event batch lacks a leading durable ACK")
            recovery = self.cursor.recover_batch(snapshot)

    async def _send_async(self, run_id):
        snapshot = await self._snapshot_async()
        if snapshot is None:
            return
        recovery = self.cursor.recover_batch(snapshot)
        if recovery and recovery.run_id != run_id:
            raise RSIRecoveryRequired(f"Session has unsent events for Run {recovery.run_id}")
        while True:
            events = recovery.events if recovery else self.cursor.pending(snapshot)
            if not events:
                return
            self.cursor.begin_batch(run_id, snapshot, events)
            receipt = await self.client.atoms.session_events(run_id, events)
            if self.cursor.ack(snapshot, receipt) == 0:
                raise RSIRecoveryRequired("Session event batch lacks a leading durable ACK")
            recovery = self.cursor.recover_batch(snapshot)

    def _visible_events(self, state, response, snapshot):
        recent = snapshot.items[state.get("session_before_count", 0):] if snapshot else ()
        roles = {item.get("role") for item in recent}
        events = []
        if "user" not in roles and state.get("visible_input"):
            events.append({
                "id": f"visible:{state['turn_id']}:input", "kind": "user_prompt",
                "payload": {"session_id": snapshot.session_id if snapshot else None, "role": "user", "content": state["visible_input"], "source_revision": "native_request"},
            })
        if "assistant" not in roles:
            events.append({
                "id": f"visible:{state['turn_id']}:output", "kind": "agent_action",
                "payload": {"session_id": snapshot.session_id if snapshot else None, "role": "assistant", "content": summary(response), "source_revision": "native_response"},
            })
        return events

    @staticmethod
    def _tool_output_events(state, kwargs):
        value = kwargs.get("input")
        if not isinstance(value, list):
            return []
        events = []
        for position, item in enumerate(value):
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                call_id = str(item.get("call_id") or position)
                events.append({
                    "id": f"visible:{state['turn_id']}:tool:{call_id}", "kind": "tool_result",
                    "payload": {"role": "tool", "content": item.get("output"), "call_id": call_id, "source_revision": "native_function_call_output"},
                })
        return events

    @staticmethod
    def _function_call_events(state, response):
        events = []
        for position, item in enumerate(getattr(response, "output", None) or []):
            kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if kind != "function_call":
                continue
            content = item if isinstance(item, dict) else vars(item)
            call_id = str(content.get("call_id") or position)
            events.append({
                "id": f"visible:{state['turn_id']}:call:{call_id}", "kind": "agent_action",
                "payload": {"role": "assistant", "content": content, "call_id": call_id, "source_revision": "native_function_call"},
            })
        return events

    def _send_observable_sync(self, state, events):
        acked = set(state.get("observable_acked_ids", []))
        pending = state.get("observable_pending") or [event for event in events if event["id"] not in acked]
        if not pending:
            return
        state["observable_pending"] = pending
        self._save(state)
        receipt = self.client.sync_atoms.session_events(state["run_id"], pending)
        ids = receipt.get("durable_ack")
        if not isinstance(ids, list):
            raise RSIRecoveryRequired("Observable model events have no durable ACK list")
        acked.update(ids)
        state["observable_acked_ids"] = sorted(acked)
        state["observable_pending"] = [event for event in pending if event["id"] not in acked]
        self._save(state)
        if state["observable_pending"]:
            raise RSIRecoveryRequired("Observable model events received partial ACK")

    async def _send_observable_async(self, state, events):
        acked = set(state.get("observable_acked_ids", []))
        pending = state.get("observable_pending") or [event for event in events if event["id"] not in acked]
        if not pending:
            return
        state["observable_pending"] = pending
        self._save(state)
        receipt = await self.client.atoms.session_events(state["run_id"], pending)
        ids = receipt.get("durable_ack")
        if not isinstance(ids, list):
            raise RSIRecoveryRequired("Observable model events have no durable ACK list")
        acked.update(ids)
        state["observable_acked_ids"] = sorted(acked)
        state["observable_pending"] = [event for event in pending if event["id"] not in acked]
        self._save(state)
        if state["observable_pending"]:
            raise RSIRecoveryRequired("Observable model events received partial ACK")

    def _send_visible_sync(self, state, response):
        snapshot = self._snapshot_sync()
        self._send_observable_sync(state, self._visible_events(state, response, snapshot))

    async def _send_visible_async(self, state, response):
        snapshot = await self._snapshot_async()
        await self._send_observable_async(state, self._visible_events(state, response, snapshot))

    def _prepare_sync(self, decision, kwargs):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs.get("previous_response_id"))
        self._save(state)
        try:
            if state["run_id"]:
                run = self.client.sync_atoms.get_run(state["run_id"])
            else:
                prepared = self.client.sync_atoms.prepare(objective=state["objective"], task_family=state["task_family"] or "general", key=state["turn_id"])
                if prepared.status == "processing":
                    raise RSIRecoveryRequired("Run preparation is still processing")
                state["run_id"] = prepared.run_id
                self._save(state)
                run = self.client.sync_atoms.get_run(prepared.run_id)
                run["recipe"] = prepared.get("recipe")
                run["context"] = prepared.get("context")
            self._send_sync(state["run_id"])
            return state, run
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    async def _prepare_async(self, decision, kwargs):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs.get("previous_response_id"))
        self._save(state)
        try:
            if state["run_id"]:
                run = await self.client.atoms.get_run(state["run_id"])
            else:
                prepared = await self.client.atoms.prepare(objective=state["objective"], task_family=state["task_family"] or "general", key=state["turn_id"])
                if prepared.status == "processing":
                    raise RSIRecoveryRequired("Run preparation is still processing")
                state["run_id"] = prepared.run_id
                self._save(state)
                run = await self.client.atoms.get_run(prepared.run_id)
                run["recipe"] = prepared.get("recipe")
                run["context"] = prepared.get("context")
            await self._send_async(state["run_id"])
            return state, run
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    def _complete_sync(self, state, response):
        response_id = getattr(response, "id", None)
        if not terminal(response):
            try:
                visible = self._visible_events(state, response, self._snapshot_sync())
                prompt_events = [event for event in visible if event["kind"] == "user_prompt"]
                self._send_observable_sync(state, prompt_events + self._function_call_events(state, response))
            except Exception as exc:
                state["last_error"] = str(exc)
                state["state"] = "observability_pending"
                self._save(state)
                return
            state["state"] = "awaiting_continuation"
            self._save(state)
            if response_id:
                self.turns.put("response:" + str(response_id), state)
            return
        state["state"] = "native_completed"
        state["result"] = {"summary": summary(response), "outcome": "completed"}
        self._save(state)
        try:
            self._send_sync(state["run_id"])
            self._send_visible_sync(state, response)
            receipt = self.client.sync_atoms.submit(state["run_id"], data=state["result"])
            if receipt.status != "completed":
                raise RSIRecoveryRequired("CoSens submit did not confirm completion")
            state["state"] = "completed"
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
        self._save(state)

    async def _complete_async(self, state, response):
        response_id = getattr(response, "id", None)
        if not terminal(response):
            try:
                visible = self._visible_events(state, response, await self._snapshot_async())
                prompt_events = [event for event in visible if event["kind"] == "user_prompt"]
                await self._send_observable_async(state, prompt_events + self._function_call_events(state, response))
            except Exception as exc:
                state["last_error"] = str(exc)
                state["state"] = "observability_pending"
                self._save(state)
                return
            state["state"] = "awaiting_continuation"
            self._save(state)
            if response_id:
                self.turns.put("response:" + str(response_id), state)
            return
        state["state"] = "native_completed"
        state["result"] = {"summary": summary(response), "outcome": "completed"}
        self._save(state)
        try:
            await self._send_async(state["run_id"])
            await self._send_visible_async(state, response)
            receipt = await self.client.atoms.submit(state["run_id"], data=state["result"])
            if receipt.status != "completed":
                raise RSIRecoveryRequired("CoSens submit did not confirm completion")
            state["state"] = "completed"
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
        self._save(state)

    def invoke_sync(self, native, path, method, args, kwargs):
        if self.mode == "disable":
            self.status.rsi_capture = "disabled"
            return method(*args, **kwargs)
        if args:
            self.status.rsi_capture = "unsupported"
            return method(*args, **kwargs)
        try:
            decision = decide_sync(native, self.decision_model, user_text(path, kwargs), tool_names(kwargs))
        except Exception as exc:
            if self.mode == "approval":
                raise RSIDecisionUnavailable(str(exc)) from exc
            self.status.rsi_capture = "decision_failed"
            return method(*args, **kwargs)
        if not decision["activate"]:
            self.status.rsi_capture = "skipped"
            return method(*args, **kwargs)
        if self.mode == "approval":
            raise RSIApprovalUnavailable("The configured CoSens service has no /v1/invocations approval endpoint")
        if not decision["objective"].strip():
            decision["objective"] = user_text(path, kwargs)
        state, run = self._prepare_sync(decision, kwargs)
        state["visible_input"] = user_text(path, kwargs)
        before = self._snapshot_sync()
        state["session_before_count"] = len(before.items) if before else 0
        self._save(state)
        self._send_observable_sync(state, self._tool_output_events(state, kwargs))
        updated = inject(path, kwargs, run)
        state["state"] = "native_running"
        self._save(state)
        try:
            response = method(**updated)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        if "with_raw_response" in path or "with_streaming_response" in path:
            self.status.rsi_capture = "awaiting_parse"
            return _RawResponse(response, self, state, asynchronous=False)
        if kwargs.get("stream") is True or base_path(path)[-1] == "stream":
            self.status.rsi_capture = "streaming"
            return _GenerationStream(response, self, state, asynchronous=False)
        self._complete_sync(state, response)
        self.status.rsi_capture = state["state"]
        return response

    async def invoke_async(self, native, path, method, args, kwargs):
        if self.mode == "disable":
            self.status.rsi_capture = "disabled"
            return await method(*args, **kwargs)
        if args:
            self.status.rsi_capture = "unsupported"
            return await method(*args, **kwargs)
        try:
            decision = await decide_async(native, self.decision_model, user_text(path, kwargs), tool_names(kwargs))
        except Exception as exc:
            if self.mode == "approval":
                raise RSIDecisionUnavailable(str(exc)) from exc
            self.status.rsi_capture = "decision_failed"
            return await method(*args, **kwargs)
        if not decision["activate"]:
            self.status.rsi_capture = "skipped"
            return await method(*args, **kwargs)
        if self.mode == "approval":
            raise RSIApprovalUnavailable("The configured CoSens service has no /v1/invocations approval endpoint")
        if not decision["objective"].strip():
            decision["objective"] = user_text(path, kwargs)
        state, run = await self._prepare_async(decision, kwargs)
        state["visible_input"] = user_text(path, kwargs)
        before = await self._snapshot_async()
        state["session_before_count"] = len(before.items) if before else 0
        self._save(state)
        await self._send_observable_async(state, self._tool_output_events(state, kwargs))
        updated = inject(path, kwargs, run)
        state["state"] = "native_running"
        self._save(state)
        try:
            response = await method(**updated)
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            self._save(state)
            raise
        if "with_raw_response" in path or "with_streaming_response" in path:
            self.status.rsi_capture = "awaiting_parse"
            return _RawResponse(response, self, state, asynchronous=True)
        if kwargs.get("stream") is True or base_path(path)[-1] == "stream":
            self.status.rsi_capture = "streaming"
            return _GenerationStream(response, self, state, asynchronous=True)
        await self._complete_async(state, response)
        self.status.rsi_capture = state["state"]
        return response


class _ResourceProxy:
    def __init__(self, native, runtime, path, root):
        self._native = native
        self._runtime = runtime
        self._path = path
        self._root = root

    def __dir__(self):
        return sorted(set(dir(type(self)) + list(self.__dict__) + dir(self._native)))

    def __getattr__(self, name):
        value = getattr(self._native, name)
        path = self._path + (name,)
        if callable(value):
            if base_path(path) in SUPPORTED:
                try:
                    from openai import AsyncOpenAI
                    async_client = isinstance(self._root, AsyncOpenAI)
                except ImportError:
                    async_client = False
                if async_client and not inspect.iscoroutinefunction(value) and (
                    base_path(path)[-1] == "stream" or "with_streaming_response" in path
                ):
                    @functools.wraps(value)
                    def async_manager(*args, **kwargs):
                        return _DeferredAsyncManager(self._root, value, path, args, kwargs, self._runtime)
                    return async_manager
                if inspect.iscoroutinefunction(value):
                    @functools.wraps(value)
                    async def async_call(*args, **kwargs):
                        return await self._runtime.invoke_async(self._root, path, value, args, kwargs)
                    return async_call

                @functools.wraps(value)
                def sync_call(*args, **kwargs):
                    return self._runtime.invoke_sync(self._root, path, value, args, kwargs)
                return sync_call
            return value
        if _resource(value):
            return _ResourceProxy(value, self._runtime, path, self._root)
        return value


class CoSensOpenAIProxy(_ResourceProxy):
    def __init__(self, native, runtime):
        super().__init__(native, runtime, (), native)
        self.native = native
        self.cosens = runtime.status

    def __getattr__(self, name):
        if name in {"copy", "with_options"}:
            method = getattr(self.native, name)
            @functools.wraps(method)
            def derive(*args, **kwargs):
                return CoSensOpenAIProxy(method(*args, **kwargs), self._runtime)
            return derive
        if name == "close":
            def close():
                return self.native.close()
            return close
        return super().__getattr__(name)

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self.native.__exit__(exc_type, exc, tb)

    async def __aenter__(self):
        await self.native.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self.native.__aexit__(exc_type, exc, tb)
