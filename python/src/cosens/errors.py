"""Stable SDK errors preserving useful server diagnostics."""


class RSIError(Exception):
    """Base class for CoSens failures."""


class RSIHTTPError(RSIError):
    def __init__(self, status_code: int, message: str, *, code: str | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id


class RSITransportError(RSIError):
    """The server response was unavailable or malformed."""


class RSIDecisionUnavailable(RSIError):
    """The classifier could not make a required decision."""


class RSIApprovalUnavailable(RSIError):
    """The configured server does not support coordinated approvals."""


class RSIApprovalRequired(RSIError):
    """A coordinated invocation is awaiting human approval."""


class RSIRecoveryRequired(RSIError):
    """A prior side effect may have happened and requires reconciliation."""


class SessionHistoryChanged(RSIError):
    """A previously acknowledged Session prefix was rewritten."""
