"""Dynamic OpenAI resource proxy with narrow RSI generation hooks."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import contextvars
import functools
import hashlib
import inspect
import json
import threading
import uuid

from .atoms import CoSensClient
from .decision import decide_async, decide_sync
from .errors import RSIApprovalUnavailable, RSIDecisionUnavailable, RSIRecoveryRequired
from .responses_adapter import SUPPORTED, base_path, inject, summary, terminal, tool_names, user_text
from .session_cursor import CursorStore
from .state import TurnStore
from .models import result
from .workflow import freeze_recall_context, freeze_run_context, prepare_ready_async, prepare_ready_sync


def _resource(value):
    module = type(value).__module__
    return module.startswith("openai.resources") or module.startswith("openai._response")


def _sync_await(coroutine_factory):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(coroutine_factory())).result()


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

    def reconcile(self, turn_id):
        if self._runtime.asynchronous:
            return self._runtime.reconcile_async(turn_id)
        return self._runtime.reconcile_sync(turn_id)

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
        if name == "close":
            closer = getattr(self._native, name)
            if inspect.iscoroutinefunction(closer):
                async def async_close():
                    try:
                        return await closer()
                    finally:
                        self._incomplete()
                return async_close
            def sync_close():
                try:
                    return closer()
                finally:
                    self._incomplete()
            return sync_close
        if name == "parse":
            parse = getattr(self._native, name)
            if inspect.iscoroutinefunction(parse):
                async def async_parse(*args, **kwargs):
                    value = await parse(*args, **kwargs)
                    from openai import AsyncStream
                    if isinstance(value, AsyncStream):
                        return _GenerationStream(value, self._runtime, self._state, asynchronous=True)
                    if self._state["state"] == "native_running":
                        await self._runtime._complete_async(self._state, value)
                    return value
                return async_parse

            def sync_parse(*args, **kwargs):
                value = parse(*args, **kwargs)
                from openai import AsyncStream, Stream
                if isinstance(value, (Stream, AsyncStream)):
                    return _GenerationStream(value, self._runtime, self._state, asynchronous=isinstance(value, AsyncStream))
                if self._state["state"] == "native_running":
                    self._runtime._complete_sync(self._state, value)
                return value
            return sync_parse
        return getattr(self._native, name)

    def _incomplete(self):
        if self._state["state"] == "native_running":
            self._state["state"] = "incomplete"
            self._runtime._save(self._state)

    def __enter__(self):
        entered = self._native.__enter__()
        return _RawResponse(entered, self._runtime, self._state, asynchronous=False)

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._native.__exit__(exc_type, exc, tb)
        finally:
            self._incomplete()

    async def __aenter__(self):
        entered = await self._native.__aenter__()
        return _RawResponse(entered, self._runtime, self._state, asynchronous=True)

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._native.__aexit__(exc_type, exc, tb)
        finally:
            self._incomplete()


class _GenerationStream:
    def __init__(self, native, runtime, state, *, asynchronous):
        self._native = native
        self._runtime = runtime
        self._state = state
        self._asynchronous = asynchronous
        self._complete_response = None
        self._finished = False
        self._text = []
        self._iterator = None
        self._async_iterator = None

    def __getattr__(self, name):
        value = getattr(self._native, name)
        if name not in {"get_final_response", "get_final_completion", "until_done"}:
            return value
        if inspect.iscoroutinefunction(value):
            async def async_final(*args, **kwargs):
                result = await value(*args, **kwargs)
                final = result
                if name == "until_done":
                    getter = getattr(self._native, "get_final_response", None) or getattr(self._native, "get_final_completion", None)
                    final = await getter() if getter else None
                if self._state["state"] == "native_running" and final is not None and terminal(final):
                    await self._runtime._complete_async(self._state, final)
                    self._finished = True
                return self if name == "until_done" else result
            return async_final

        def sync_final(*args, **kwargs):
            result = value(*args, **kwargs)
            final = result
            if name == "until_done":
                getter = getattr(self._native, "get_final_response", None) or getattr(self._native, "get_final_completion", None)
                final = getter() if getter else None
            if self._state["state"] == "native_running" and final is not None and terminal(final):
                self._runtime._complete_sync(self._state, final)
                self._finished = True
            return self if name == "until_done" else result
        return sync_final

    def _inspect_event(self, event):
        event_type = getattr(event, "type", None)
        if event_type == "response.completed":
            self._complete_response = getattr(event, "response", None)
        chunk = getattr(event, "chunk", event)
        for choice in getattr(chunk, "choices", []) or []:
            delta = getattr(choice, "delta", None)
            content = getattr(delta, "content", None)
            if content:
                self._text.append(content)
            if getattr(choice, "finish_reason", None) not in {None, "tool_calls", "function_call"}:
                from types import SimpleNamespace
                self._complete_response = SimpleNamespace(
                    id=getattr(chunk, "id", None),
                    status="completed",
                    choices=[SimpleNamespace(finish_reason=choice.finish_reason, message=SimpleNamespace(content="".join(self._text)))],
                )

    def _incomplete(self):
        if not self._finished and self._state["state"] == "native_running":
            self._state["state"] = "incomplete"
            self._runtime._save(self._state)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            if self._iterator is None:
                self._iterator = iter(self._native)
            event = next(self._iterator)
        except StopIteration:
            self._incomplete()
            raise
        except BaseException:
            self._incomplete()
            raise
        self._inspect_event(event)
        if self._complete_response is not None and self._state["state"] == "native_running":
            self._runtime._complete_sync(self._state, self._complete_response)
            self._finished = True
        return event

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            if self._async_iterator is None:
                self._async_iterator = self._native.__aiter__()
            event = await anext(self._async_iterator)
        except StopAsyncIteration:
            self._incomplete()
            raise
        except BaseException:
            self._incomplete()
            raise
        self._inspect_event(event)
        if self._complete_response is not None and self._state["state"] == "native_running":
            await self._runtime._complete_async(self._state, self._complete_response)
            self._finished = True
        return event

    def close(self):
        closer = self._native.close
        if inspect.iscoroutinefunction(closer):
            async def async_close():
                try:
                    return await closer()
                finally:
                    self._incomplete()
            return async_close()
        try:
            return closer()
        finally:
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
        self._entered_response = None

    async def __aenter__(self):
        runtime = self._runtime
        if self._args:
            runtime.status.rsi_capture = "unsupported"
            self._manager = self._method(*self._args, **self._kwargs)
            return await self._manager.__aenter__()
        state, updated = await runtime._preflight_async(self._client, self._path, self._kwargs)
        try:
            self._manager = self._method(**updated)
            entered = await self._manager.__aenter__()
        except Exception as exc:
            state["state"] = "native_failed"
            state["last_error"] = str(exc)
            runtime._save(state)
            raise
        if "with_streaming_response" in self._path:
            self._entered_response = _RawResponse(entered, runtime, state, asynchronous=True)
        else:
            self._entered_response = _GenerationStream(entered, runtime, state, asynchronous=True)
        return self._entered_response

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._manager.__aexit__(exc_type, exc, tb)
        finally:
            if self._entered_response is not None:
                self._entered_response._incomplete()


class _Runtime:
    def __init__(self, *, mode, decision_model, session, api_key, state_path, http_transport, asynchronous=False, decision_timeout=5, decision_max_output_tokens=256):
        if mode in {"auto", "approval"} and not decision_model:
            raise ValueError("decision_model is required for auto/approval")
        self.mode = mode
        self.asynchronous = asynchronous
        self.decision_model = decision_model
        if decision_timeout <= 0 or decision_max_output_tokens <= 0:
            raise ValueError("Decision timeout and output token limit must be positive")
        self.decision_timeout = decision_timeout
        self.decision_max_output_tokens = decision_max_output_tokens
        self.session = session
        self.client = CoSensClient(api_key=api_key, state_path=state_path, _http_transport=http_transport)
        self.cursor = CursorStore(state_path, principal=self.client._transport.api_key)
        self.turns = TurnStore(state_path, principal=self.client._transport.api_key)
        self.status = _Status(self)
        self._owner_lock = threading.Lock()
        self._owners = 0
        self._closed = False

    def acquire(self):
        with self._owner_lock:
            if self._closed:
                raise RuntimeError("CoSens client is closed")
            self._owners += 1

    def _drop_owner(self):
        with self._owner_lock:
            if self._owners <= 0:
                return False
            self._owners -= 1
            if self._owners or self._closed:
                return False
            self._closed = True
            return True

    async def release_async(self):
        if self._drop_owner():
            try:
                await self.client.aclose()
            finally:
                self.cursor.close()
                self.turns.close()

    def release_sync(self):
        if self._drop_owner():
            try:
                _sync_await(self.client.aclose)
            finally:
                self.cursor.close()
                self.turns.close()

    def _request_key(self, kwargs, path):
        scope = self.status._scope.get()
        session_id = (scope[0] if scope and scope[0] else None) or getattr(self.session, "session_id", None) or getattr(self.session, "id", None)
        incarnation = getattr(self.session, "session_incarnation", None) or getattr(self.session, "incarnation", "0")
        signature = json.dumps([str(session_id), str(incarnation), path, kwargs], sort_keys=True, default=str, ensure_ascii=False)
        return "request:" + hashlib.sha256(signature.encode()).hexdigest()

    def _existing_request(self, kwargs, path):
        scope = self.status._scope.get()
        return self.turns.get(scope[1]) if scope and scope[1] else self.turns.get(self._request_key(kwargs, path))

    def _conversation_key(self, kwargs):
        conversation = kwargs.get("conversation")
        if isinstance(conversation, dict):
            conversation = conversation.get("id")
        if not conversation:
            return None
        session_id = getattr(self.session, "session_id", None)
        return "conversation:" + hashlib.sha256(json.dumps([session_id, conversation], default=str).encode()).hexdigest()

    def _continuation_state(self, kwargs):
        scope = self.status._scope.get()
        if scope and scope[1]:
            state = self.turns.get(scope[1])
        else:
            response_id = kwargs.get("previous_response_id")
            alias = self.turns.get("response:" + str(response_id)) if response_id else None
            inputs = kwargs.get("input")
            tool_output = isinstance(inputs, list) and any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in inputs)
            key = self._conversation_key(kwargs)
            if alias is None and key and tool_output:
                alias = self.turns.get(key)
            state = self.turns.get(alias.get("turn_ref", alias.get("turn_id"))) if alias else None
        return state if state and state.get("state") == "awaiting_continuation" else None

    def _guard_request(self, kwargs, path):
        existing = self._existing_request(kwargs, path)
        if existing and existing.get("state") == "awaiting_continuation":
            continuation = self._continuation_state(kwargs)
            if continuation and self._request_key(kwargs, path) not in existing.get("request_keys", []):
                return
        if existing and existing.get("state") not in {"pre_execution_pending", "completed"}:
            raise RSIRecoveryRequired(f"Turn {existing['turn_id']} may have executed; reconcile without repeating the model request")
        scope = self.status._scope.get()
        if existing and existing.get("state") == "completed" and scope and scope[1]:
            raise RSIRecoveryRequired(f"Turn {existing['turn_id']} is already completed")

    def _saved_decision(self, kwargs, path):
        state = self._continuation_state(kwargs) or self._existing_request(kwargs, path)
        if state and state.get("state") in {"awaiting_continuation", "pre_execution_pending"}:
            return state.get("decision")
        return None

    def _decision_result(self, decision, path, kwargs):
        if self.mode == "approval" and decision["activate"]:
            raise RSIApprovalUnavailable("The configured CoSens service has no verified /v1/invocations approval contract")
        decision = dict(decision)
        if not decision["objective"].strip():
            decision["objective"] = user_text(path, kwargs)
        decision.setdefault("capture", "active" if decision["activate"] else "skipped")
        self.status.rsi_capture = decision["capture"]
        return decision

    def _no_run_decision(self, path, kwargs, capture):
        return {"activate": False, "objective": user_text(path, kwargs), "task_family": "general", "reason": capture, "capture": capture}

    def _decide_sync(self, native, path, kwargs):
        saved = self._saved_decision(kwargs, path)
        if saved:
            return saved
        if self.mode == "disable":
            return self._no_run_decision(path, kwargs, "disabled")
        try:
            decision = decide_sync(native, self.decision_model, user_text(path, kwargs), tool_names(kwargs), timeout=self.decision_timeout, max_output_tokens=self.decision_max_output_tokens)
        except Exception as exc:
            if self.mode == "approval":
                raise RSIDecisionUnavailable(str(exc)) from exc
            decision = self._no_run_decision(path, kwargs, "decision_failed")
        return self._decision_result(decision, path, kwargs)

    async def _decide_async(self, native, path, kwargs):
        saved = self._saved_decision(kwargs, path)
        if saved:
            return saved
        if self.mode == "disable":
            return self._no_run_decision(path, kwargs, "disabled")
        try:
            decision = await decide_async(native, self.decision_model, user_text(path, kwargs), tool_names(kwargs), timeout=self.decision_timeout, max_output_tokens=self.decision_max_output_tokens)
        except Exception as exc:
            if self.mode == "approval":
                raise RSIDecisionUnavailable(str(exc)) from exc
            decision = self._no_run_decision(path, kwargs, "decision_failed")
        return self._decision_result(decision, path, kwargs)

    def _new_state(self, objective, task_family, kwargs, path):
        request_key = self._request_key(kwargs, path)
        previous = self._continuation_state(kwargs)
        if previous and previous.get("state") == "awaiting_continuation":
            state = previous
        else:
            scope = self.status._scope.get()
            existing = self._existing_request(kwargs, path)
            if existing and existing.get("state") == "pre_execution_pending":
                state = existing
            elif existing and existing.get("state") != "completed":
                raise RSIRecoveryRequired(f"Turn {existing['turn_id']} may have executed; reconcile without repeating the model request")
            elif existing and scope and scope[1]:
                raise RSIRecoveryRequired(f"Turn {existing['turn_id']} is already completed")
            else:
                turn_id = scope[1] if scope and scope[1] else str(uuid.uuid4())
                state = {"turn_id": turn_id, "state": "pre_execution_pending", "objective": objective, "task_family": task_family, "run_id": None}
        state["conversation_key"] = self._conversation_key(kwargs) or state.get("conversation_key")
        keys = state.setdefault("request_keys", [])
        if request_key not in keys:
            keys.append(request_key)
        self.status.last_turn_id = state["turn_id"]
        return state

    def _save(self, state):
        if state.get("conversation_key"):
            self.turns.put(state["conversation_key"], {"turn_ref": state["turn_id"]})
        self.turns.put(state["turn_id"], state)
        for request_key in state.get("request_keys", []):
            self.turns.put(request_key, state)

    def _snapshot_sync(self):
        if self.session is None:
            return None
        return _sync_await(lambda: self.cursor.snapshot(self.session))

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
        if "user" not in roles and state.get("visible_input") and not state.get("visible_input_in_session"):
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
    def _input_in_snapshot(snapshot, visible_input):
        if not snapshot or not visible_input or not snapshot.items:
            return False
        latest = snapshot.items[-1]
        return latest.get("role") == "user" and latest.get("content") == visible_input

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

    def _prepare_sync(self, decision, kwargs, path):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs, path)
        state["decision"] = decision
        self._save(state)
        try:
            if "frozen_context" not in state:
                if not state["run_id"]:
                    prepared = prepare_ready_sync(self.client.sync_atoms, objective=state["objective"], task_family=state["task_family"] or "general", key=state["turn_id"])
                    state["run_id"] = prepared.run_id
                    state["prepared"] = dict(prepared)
                    self._save(state)
                run = self.client.sync_atoms.get_run(state["run_id"])
                state["frozen_context"] = freeze_run_context(state.get("prepared"), run)
                self._save(state)
            self._send_sync(state["run_id"])
            return state, result(state["frozen_context"])
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    async def _prepare_async(self, decision, kwargs, path):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs, path)
        state["decision"] = decision
        self._save(state)
        try:
            if "frozen_context" not in state:
                if not state["run_id"]:
                    prepared = await prepare_ready_async(self.client.atoms, objective=state["objective"], task_family=state["task_family"] or "general", key=state["turn_id"])
                    state["run_id"] = prepared.run_id
                    state["prepared"] = dict(prepared)
                    self._save(state)
                run = await self.client.atoms.get_run(state["run_id"])
                state["frozen_context"] = freeze_run_context(state.get("prepared"), run)
                self._save(state)
            await self._send_async(state["run_id"])
            return state, result(state["frozen_context"])
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    def _recall_sync(self, decision, kwargs, path):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs, path)
        state["decision"] = decision
        state["capture"] = decision["capture"]
        self._save(state)
        try:
            if "frozen_context" not in state:
                recalled = self.client.sync_atoms.recall_context(query=state["objective"])
                state["frozen_context"] = freeze_recall_context(recalled)
                self._save(state)
            return state, result(state["frozen_context"])
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    async def _recall_async(self, decision, kwargs, path):
        state = self._new_state(decision["objective"], decision["task_family"], kwargs, path)
        state["decision"] = decision
        state["capture"] = decision["capture"]
        self._save(state)
        try:
            if "frozen_context" not in state:
                recalled = await self.client.atoms.recall_context(query=state["objective"])
                state["frozen_context"] = freeze_recall_context(recalled)
                self._save(state)
            return state, result(state["frozen_context"])
        except Exception as exc:
            state["last_error"] = str(exc)
            self._save(state)
            raise

    def _preflight_sync(self, native, path, kwargs):
        self._guard_request(kwargs, path)
        decision = self._decide_sync(native, path, kwargs)
        state, context = self._prepare_sync(decision, kwargs, path) if decision["activate"] else self._recall_sync(decision, kwargs, path)
        if state["run_id"]:
            state["visible_input"] = user_text(path, kwargs)
            before = self._snapshot_sync()
            state["session_before_count"] = len(before.items) if before else 0
            state["visible_input_in_session"] = self._input_in_snapshot(before, state["visible_input"])
            self._save(state)
            self._send_observable_sync(state, self._tool_output_events(state, kwargs))
        updated = inject(path, kwargs, context)
        state["state"] = "native_running"
        self._save(state)
        return state, updated

    async def _preflight_async(self, native, path, kwargs):
        self._guard_request(kwargs, path)
        decision = await self._decide_async(native, path, kwargs)
        state, context = await self._prepare_async(decision, kwargs, path) if decision["activate"] else await self._recall_async(decision, kwargs, path)
        if state["run_id"]:
            state["visible_input"] = user_text(path, kwargs)
            before = await self._snapshot_async()
            state["session_before_count"] = len(before.items) if before else 0
            state["visible_input_in_session"] = self._input_in_snapshot(before, state["visible_input"])
            self._save(state)
            await self._send_observable_async(state, self._tool_output_events(state, kwargs))
        updated = inject(path, kwargs, context)
        state["state"] = "native_running"
        self._save(state)
        return state, updated

    def _complete_without_run(self, state, response):
        if state["run_id"] is not None:
            return False
        if terminal(response):
            state["state"] = "completed"
            state["result"] = {"summary": summary(response), "outcome": "completed"}
        else:
            state["state"] = "awaiting_continuation"
            response_id = getattr(response, "id", None)
            if response_id:
                self.turns.put("response:" + str(response_id), {"turn_ref": state["turn_id"]})
        self._save(state)
        return True

    def _complete_sync(self, state, response):
        if self._complete_without_run(state, response):
            return
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
                self.turns.put("response:" + str(response_id), {"turn_ref": state["turn_id"]})
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
        if self._complete_without_run(state, response):
            return
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
                self.turns.put("response:" + str(response_id), {"turn_ref": state["turn_id"]})
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

    def reconcile_sync(self, turn_id):
        state = self.turns.get(turn_id)
        if not state or state.get("state") not in {"native_completed", "completion_pending"}:
            raise RSIRecoveryRequired("Turn has no completed native result to reconcile")
        try:
            self._send_sync(state["run_id"])
            self._send_observable_sync(state, [])
            remote = self.client.sync_atoms.get_run(state["run_id"])
            if remote.status == "completed":
                if remote.get("result") != state.get("result"):
                    raise RSIRecoveryRequired("Remote completed Run result differs from locally saved result")
            elif remote.status == "ready":
                receipt = self.client.sync_atoms.submit(state["run_id"], data=state["result"])
                if receipt.status != "completed":
                    raise RSIRecoveryRequired("CoSens did not confirm recovered submit")
            else:
                raise RSIRecoveryRequired(f"Run {state['run_id']} is {remote.status}; cannot safely submit")
            state["state"] = "completed"
            state.pop("last_error", None)
            self._save(state)
            return state
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)
            raise

    async def reconcile_async(self, turn_id):
        state = self.turns.get(turn_id)
        if not state or state.get("state") not in {"native_completed", "completion_pending"}:
            raise RSIRecoveryRequired("Turn has no completed native result to reconcile")
        try:
            await self._send_async(state["run_id"])
            await self._send_observable_async(state, [])
            remote = await self.client.atoms.get_run(state["run_id"])
            if remote.status == "completed":
                if remote.get("result") != state.get("result"):
                    raise RSIRecoveryRequired("Remote completed Run result differs from locally saved result")
            elif remote.status == "ready":
                receipt = await self.client.atoms.submit(state["run_id"], data=state["result"])
                if receipt.status != "completed":
                    raise RSIRecoveryRequired("CoSens did not confirm recovered submit")
            else:
                raise RSIRecoveryRequired(f"Run {state['run_id']} is {remote.status}; cannot safely submit")
            state["state"] = "completed"
            state.pop("last_error", None)
            self._save(state)
            return state
        except Exception as exc:
            state["state"] = "completion_pending"
            state["last_error"] = str(exc)
            self._save(state)
            raise

    def invoke_sync(self, native, path, method, args, kwargs):
        if args or base_path(path) == ("completions", "create") or (
            base_path(path)[0] == "responses" and kwargs.get("prompt") and not kwargs.get("input")
        ):
            self.status.rsi_capture = "unsupported"
            return method(*args, **kwargs)
        state, updated = self._preflight_sync(native, path, kwargs)
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
        self.status.rsi_capture = state.get("capture") or state["state"]
        return response

    async def invoke_async(self, native, path, method, args, kwargs):
        if args or base_path(path) == ("completions", "create") or (
            base_path(path)[0] == "responses" and kwargs.get("prompt") and not kwargs.get("input")
        ):
            self.status.rsi_capture = "unsupported"
            return await method(*args, **kwargs)
        state, updated = await self._preflight_async(native, path, kwargs)
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
        self.status.rsi_capture = state.get("capture") or state["state"]
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
            capture = "unsupported" if base_path(path)[0] in {"responses", "chat", "completions", "realtime", "batches"} else "not_applicable"
            if inspect.iscoroutinefunction(value):
                @functools.wraps(value)
                async def async_passthrough(*args, **kwargs):
                    self._runtime.status.rsi_capture = capture
                    return await value(*args, **kwargs)
                return async_passthrough

            @functools.wraps(value)
            def passthrough(*args, **kwargs):
                self._runtime.status.rsi_capture = capture
                return value(*args, **kwargs)
            return passthrough
        if _resource(value):
            return _ResourceProxy(value, self._runtime, path, self._root)
        return value


class CoSensOpenAIProxy(_ResourceProxy):
    def __init__(self, native, runtime):
        super().__init__(native, runtime, (), native)
        self.native = native
        self.cosens = runtime.status
        self._closed = False
        runtime.acquire()

    def _release_sync(self):
        if not self._closed:
            self._closed = True
            self._runtime.release_sync()

    async def _release_async(self):
        if not self._closed:
            self._closed = True
            await self._runtime.release_async()

    def __getattr__(self, name):
        if name in {"copy", "with_options"}:
            method = getattr(self.native, name)
            @functools.wraps(method)
            def derive(*args, **kwargs):
                return CoSensOpenAIProxy(method(*args, **kwargs), self._runtime)
            return derive
        if name == "close":
            if inspect.iscoroutinefunction(self.native.close):
                async def async_close():
                    try:
                        return await self.native.close()
                    finally:
                        await self._release_async()
                return async_close
            def close():
                try:
                    return self.native.close()
                finally:
                    self._release_sync()
            return close
        return super().__getattr__(name)

    def __enter__(self):
        self.native.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return self.native.__exit__(exc_type, exc, tb)
        finally:
            self._release_sync()

    async def __aenter__(self):
        await self.native.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self.native.__aexit__(exc_type, exc, tb)
        finally:
            await self._release_async()
