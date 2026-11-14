"""Stable SDK errors preserving useful server diagnostics."""


class RSIError(Exception):
    """Base class for CoSens failures."""


class RSIHTTPError(RSIError):
    def __init__(self, status_code: int, message: str, *, code: str | None = None, request_id: str | None = None,
                 current_version: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.current_version = current_version  # a Recipe version conflict reports the chain's current version


class RSITransportError(RSIError):
    """The server response was unavailable or malformed."""


class RSIRecallUnavailable(RSIError):
    """The public standalone Recipe/Memory recall contract is not available yet."""


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


class RSILearningFailed(RSIError):
    """An execution's learning ended ``failed``.

    ``code`` is the server's stable failure code. ``retryable`` is True when
    ``resume_learning`` may succeed and False otherwise, including when the
    learning carries no ``error_detail``.
    """

    def __init__(self, execution_id, code, message, retryable=False, phase=None, upstream_status=None,
                 upstream_code=None, *, request_id=None):
        super().__init__(f"Learning failed for execution {execution_id}: {code}")
        self.execution_id = execution_id
        self.code = code
        self.message = message
        self.retryable = retryable
        self.phase = phase
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.request_id = request_id

    @classmethod
    def from_learning(cls, learning, *, request_id=None):
        """Build the error from a failed learning view as returned by the service."""
        detail = learning.get("error_detail") or {}
        code = detail.get("code") or learning.get("error") or "failed"
        return cls(learning.get("execution_id"), code, detail.get("message") or code,
                   retryable=detail.get("retryable") is True, phase=detail.get("phase", learning.get("phase")),
                   upstream_status=detail.get("upstream_status"), upstream_code=detail.get("upstream_code"),
                   request_id=request_id)


_STOPPED = {
    "interrupted": "was interrupted; call resume_learning to continue it",
    "awaiting_runner": "is waiting for a model; call resume_learning with a model",
    "awaiting_material": "kept the current Recipe version ({gap}); learning more needs feedback or evidence "
                         "in a new execution with previous_execution_id",
}


class RSILearningStopped(RSIError):
    """An execution's learning stopped in a state that only the caller can change.

    ``status`` is ``interrupted``, ``awaiting_runner`` or ``awaiting_material``;
    ``learning`` is the learning view. This is not a failure: ``RSILearningFailed``
    is raised for ``failed``.
    """

    def __init__(self, learning, *, request_id=None):
        self.learning = learning
        self.execution_id = learning.get("execution_id")
        self.status = learning.get("status")
        self.request_id = request_id
        action = _STOPPED[self.status].format(gap=learning.get("material_gap"))
        super().__init__(f"Learning for execution {self.execution_id} {action}")
