export class RSIError extends Error { name = 'RSIError'; }
export class RSIHTTPError extends RSIError {
  name = 'RSIHTTPError';
  constructor(public statusCode: number, message: string, public code?: string, public requestId?: string) { super(message); }
}
export class RSITransportError extends RSIError { name = 'RSITransportError'; }
export class RSIDecisionUnavailable extends RSIError { name = 'RSIDecisionUnavailable'; }
export class RSIApprovalUnavailable extends RSIError { name = 'RSIApprovalUnavailable'; }
export class RSIRecoveryRequired extends RSIError { name = 'RSIRecoveryRequired'; }
export class SessionHistoryChanged extends RSIError { name = 'SessionHistoryChanged'; }

/**
 * A Recipe candidate ended in `failed`. `code` is the server's stable failure code;
 * `retryable` is true when resuming may succeed, false when it will not, and
 * undefined when the server did not say (servers without `error_detail`).
 */
export class RSICandidateFailedError extends RSIError {
  name = 'RSICandidateFailedError';
  constructor(message: string, public code: string, public candidateId?: string, public phase?: string,
    public retryable?: boolean, public upstreamStatus?: number, public upstreamCode?: string, public requestId?: string) { super(message); }

  /** Build the error from a failed candidate as returned by the service. */
  static fromCandidate(candidate: any): RSICandidateFailedError {
    const id = candidate?.recipe_candidate_id;
    const requestId = candidate?.requestId;
    const detail = candidate?.error_detail;
    if (detail && typeof detail === 'object') {
      const code = detail.code ?? candidate.error ?? 'failed';
      return new RSICandidateFailedError(`Recipe candidate ${id} failed: ${detail.message ?? code}`, code, id,
        detail.phase ?? candidate.phase, detail.retryable ?? undefined, detail.upstream_status ?? undefined, detail.upstream_code ?? undefined, requestId);
    }
    // Servers before error_detail report only a bare code in `error`.
    const code = candidate?.error ?? 'failed';
    return new RSICandidateFailedError(`Recipe candidate ${id} failed: ${code}`, code, id, candidate?.phase, undefined, undefined, undefined, requestId);
  }
}
