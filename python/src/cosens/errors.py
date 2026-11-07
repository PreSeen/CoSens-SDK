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


class RSICandidateFailed(RSIError):
    """A Recipe candidate ended in ``failed``.

    ``code`` is the server's stable failure code. ``retryable`` is True when
    ``resume_recipe_candidate`` may succeed, False when it will not, and None
    when the server did not say (servers without ``error_detail``).
    """

    def __init__(self, message, *, code, candidate_id=None, phase=None, retryable=None,
                 upstream_status=None, upstream_code=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.candidate_id = candidate_id
        self.phase = phase
        self.retryable = retryable
        self.upstream_status = upstream_status
        self.upstream_code = upstream_code
        self.request_id = request_id

    @classmethod
    def from_candidate(cls, candidate):
        """Build the error from a failed candidate as returned by the service."""
        candidate_id = candidate.get("recipe_candidate_id")
        detail = candidate.get("error_detail")
        if isinstance(detail, dict):
            code = detail.get("code") or candidate.get("error") or "failed"
            message = detail.get("message") or code
            return cls(f"Recipe candidate {candidate_id} failed: {message}", code=code, candidate_id=candidate_id,
                       phase=detail.get("phase", candidate.get("phase")), retryable=detail.get("retryable"),
                       upstream_status=detail.get("upstream_status"), upstream_code=detail.get("upstream_code"),
                       request_id=getattr(candidate, "request_id", None))
        # Servers before error_detail report only a bare code in ``error``.
        code = candidate.get("error") or "failed"
        return cls(f"Recipe candidate {candidate_id} failed: {code}", code=code, candidate_id=candidate_id,
                   phase=candidate.get("phase"), request_id=getattr(candidate, "request_id", None))
