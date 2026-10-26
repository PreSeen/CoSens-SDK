"""Entry point dispatching existing native clients to CoSens wrappers."""

from .agents_adapter import CoSensAgentRunner


def cosens(native, *, mode="auto", decision_model=None, session=None, token=None, state_path=None, _http_transport=None, _runner=None):
    if mode not in {"auto", "approval", "disable"}:
        raise ValueError("mode must be auto, approval, or disable")
    if hasattr(native, "cosens") and hasattr(native, "native"):
        if native.cosens.mode == mode and native.cosens.session is session:
            return native
        raise ValueError("CoSens proxy is already configured differently")
    try:
        from agents import Agent
    except ImportError:
        Agent = None
    if Agent is not None and isinstance(native, Agent):
        return CoSensAgentRunner(
            native, mode=mode, session=session, token=token,
            state_path=state_path, http_transport=_http_transport, runner=_runner,
        )
    raise TypeError("Expected an OpenAI client or an agents.Agent")
