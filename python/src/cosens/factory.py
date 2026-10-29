"""Entry point dispatching existing native clients to CoSens wrappers."""

from .agents_adapter import CoSensAgentRunner
from .openai_proxy import CoSensOpenAIProxy, _Runtime
from .task_runner import TaskWorkflowRunner


def cosens(native, *, api_key=None, mode=None, decision_model=None, decision_timeout=5, decision_max_output_tokens=256, session=None, state_path=None, result_adapter=None, trace_adapter=None, on_rsi_event=None, _http_transport=None, _runner=None):
    if mode is not None and mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval, or disable")
    if hasattr(native, "cosens") and hasattr(native, "native"):
        same = native.cosens.mode == (mode or native.cosens.mode) and native.cosens.session is session
        if isinstance(native, CoSensOpenAIProxy):
            same = same and (decision_model is None or decision_model == native._runtime.decision_model)
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
        return TaskWorkflowRunner(native, mode=mode or "disable", api_key=api_key, state_path=state_path,
                                  result_adapter=result_adapter, trace_adapter=trace_adapter,
                                  on_rsi_event=on_rsi_event, _http_transport=_http_transport)
    if Agent is not None and isinstance(native, Agent):
        return CoSensAgentRunner(
            native, mode=mode or "auto", session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport, runner=_runner,
        )
    try:
        from openai import OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI
    except ImportError:
        OpenAI = AsyncOpenAI = AzureOpenAI = AsyncAzureOpenAI = None
    if OpenAI is not None and isinstance(native, (OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI)):
        runtime = _Runtime(
            mode=mode or "auto", decision_model=decision_model, session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport,
            asynchronous=isinstance(native, AsyncOpenAI),
            decision_timeout=decision_timeout, decision_max_output_tokens=decision_max_output_tokens,
        )
        return CoSensOpenAIProxy(native, runtime)
    raise TypeError("Expected an OpenAI client, agents.Agent or agents.Runner")
