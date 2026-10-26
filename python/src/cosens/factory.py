"""Entry point dispatching existing native clients to CoSens wrappers."""

from .agents_adapter import CoSensAgentRunner
from .openai_proxy import CoSensOpenAIProxy, _Runtime


def cosens(native, *, mode="auto", decision_model=None, decision_timeout=5, decision_max_output_tokens=256, session=None, api_key=None, state_path=None, _http_transport=None, _runner=None):
    if mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval, or disable")
    if hasattr(native, "cosens") and hasattr(native, "native"):
        same = native.cosens.mode == mode and native.cosens.session is session
        if isinstance(native, CoSensOpenAIProxy):
            same = same and (decision_model is None or decision_model == native._runtime.decision_model)
            same = same and (api_key is None or api_key == native._runtime.client._transport.api_key)
        elif isinstance(native, CoSensAgentRunner):
            same = same and (api_key is None or api_key == native._client._transport.api_key)
        if same:
            return native
        raise ValueError("CoSens proxy is already configured differently")
    try:
        from agents import Agent
    except ImportError:
        Agent = None
    if Agent is not None and isinstance(native, Agent):
        return CoSensAgentRunner(
            native, mode=mode, session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport, runner=_runner,
        )
    try:
        from openai import OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI
    except ImportError:
        OpenAI = AsyncOpenAI = AzureOpenAI = AsyncAzureOpenAI = None
    if OpenAI is not None and isinstance(native, (OpenAI, AsyncOpenAI, AzureOpenAI, AsyncAzureOpenAI)):
        runtime = _Runtime(
            mode=mode, decision_model=decision_model, session=session,
            api_key=api_key, state_path=state_path, http_transport=_http_transport,
            asynchronous=isinstance(native, AsyncOpenAI),
            decision_timeout=decision_timeout, decision_max_output_tokens=decision_max_output_tokens,
        )
        return CoSensOpenAIProxy(native, runtime)
    raise TypeError("Expected an OpenAI client or an agents.Agent")
