"""Entry point dispatching existing native clients to CoSens wrappers."""

from .agents_adapter import CoSensAgentRunner
from .context_render import DEFAULT_MAX_CONTEXT_CHARS
from .decision import DEFAULT_DECISION_MAX_OUTPUT_TOKENS, DEFAULT_DECISION_TIMEOUT
from .openai_proxy import CoSensOpenAIProxy, _Runtime
from .task_runner import TaskWorkflowRunner


def cosens(native, *, api_key=None, mode=None, decision_model=None, decision_timeout=DEFAULT_DECISION_TIMEOUT,
           decision_max_output_tokens=DEFAULT_DECISION_MAX_OUTPUT_TOKENS, decision_client=None, decision_extra_body=None,
           max_context_chars=DEFAULT_MAX_CONTEXT_CHARS, auto_learning=None, session=None, state_path=None,
           result_adapter=None, trace_adapter=None, on_rsi_event=None, _http_transport=None, _runner=None):
    """Wrap an OpenAI client, ``agents.Agent`` or ``agents.Runner`` with CoSens capture.

    Decision options apply to the ``auto``/``approval`` policy call made before a
    wrapped OpenAI request: it is not retried, times out after ``decision_timeout``
    seconds and is capped at ``decision_max_output_tokens``. ``decision_client`` is
    an optional separate OpenAI client (sync or async to match ``native``) for the
    decision and Recipe-match calls; ``decision_extra_body`` is forwarded to both,
    e.g. a reasoning switch your gateway accepts. ``max_context_chars``
    bounds the Recipe/Memory text injected into requests and Agent instructions.
    """
    if mode is not None and mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval, or disable")
    if hasattr(native, "cosens") and hasattr(native, "native"):
        same = native.cosens.mode == (mode or native.cosens.mode) and native.cosens.session is session
        if isinstance(native, CoSensOpenAIProxy):
            same = same and (auto_learning is None or auto_learning == native._runtime.auto_learning)
            same = same and (decision_model is None or decision_model == native._runtime.decision_model)
            same = same and (decision_client is None or decision_client is native._runtime.decision_client_source)
            same = same and (decision_extra_body is None or decision_extra_body == native._runtime.decision_extra_body)
            same = same and (api_key is None or api_key == native._runtime.client._transport.api_key)
        elif isinstance(native, CoSensAgentRunner):
            same = same and (api_key is None or api_key == native._client._transport.api_key)
        elif isinstance(native, TaskWorkflowRunner):
            same = same and (api_key is None or api_key == native._client._transport.api_key)
            same = same and (result_adapter is None or result_adapter is native._result_adapter)
            same = same and (trace_adapter is None or trace_adapter is native._trace_adapter)
            same = same and (on_rsi_event is None or on_rsi_event is native._on_rsi_event)
        if same:
            return native
        raise ValueError("CoSens proxy is already configured differently")
    try:
        from agents import Agent, Runner
    except ImportError:
        Agent = Runner = None
    if Runner is not None and (native is Runner or isinstance(native, Runner)):
        if auto_learning not in (None, False):
            raise ValueError("auto_learning currently requires an OpenAI client")
        return TaskWorkflowRunner(native, mode=mode or "disable", api_key=api_key, state_path=state_path,
                                  result_adapter=result_adapter, trace_adapter=trace_adapter,
                                  on_rsi_event=on_rsi_event, max_context_chars=max_context_chars,
                                  _http_transport=_http_transport)
    if Agent is not None and isinstance(native, Agent):
        if auto_learning not in (None, False):
            raise ValueError("auto_learning currently requires an OpenAI client")
        return CoSensAgentRunner(
            native, mode=mode or "auto", session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport, runner=_runner,
            max_context_chars=max_context_chars,
        )
    try:
        from openai import OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI
    except ImportError:
        OpenAI = AsyncOpenAI = AzureOpenAI = AsyncAzureOpenAI = None
    if OpenAI is not None and isinstance(native, (OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI)):
        asynchronous = isinstance(native, AsyncOpenAI)
        if decision_client is not None and not isinstance(decision_client, (AsyncOpenAI,) if asynchronous else (OpenAI,)):
            raise TypeError("decision_client must be an " + ("AsyncOpenAI" if asynchronous else "OpenAI") +
                            " client, matching the wrapped client")
        runtime = _Runtime(
            mode=mode or "auto", decision_model=decision_model, session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport,
            asynchronous=asynchronous,
            decision_timeout=decision_timeout, decision_max_output_tokens=decision_max_output_tokens,
            decision_client=decision_client, decision_extra_body=decision_extra_body,
            max_context_chars=max_context_chars, auto_learning=auto_learning,
        )
        return CoSensOpenAIProxy(native, runtime)
    raise TypeError("Expected an OpenAI client, agents.Agent or agents.Runner")
