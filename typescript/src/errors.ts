export class RSIError extends Error { name = 'RSIError'; }
/** `missing` (input_incomplete) is what the execution input lacks; `recipeIds` (recipe_choice_required) are the task's Recipes; a new execution names one, a correction or learning cannot. */
export class RSIHTTPError extends RSIError {
  name = 'RSIHTTPError';
  constructor(public statusCode: number, message: string, public code?: string, public requestId?: string,
    public missing?: string[], public recipeIds?: string[]) { super(message); }
}
export class RSITransportError extends RSIError { name = 'RSITransportError'; }
export class RSIDecisionUnavailable extends RSIError { name = 'RSIDecisionUnavailable'; }
export class RSIApprovalUnavailable extends RSIError { name = 'RSIApprovalUnavailable'; }
export class RSIRecoveryRequired extends RSIError { name = 'RSIRecoveryRequired'; }
export class SessionHistoryChanged extends RSIError { name = 'SessionHistoryChanged'; }

/**
 * An execution's learning ended `failed`. `code` is the server's stable failure code;
 * `retryable` is true when `resumeLearning` may succeed, false when it will not, and
 * undefined when the learning does not say (a bare code such as `previous_version_failed`
 * has no `error_detail` and can still be resumed). `detailMessage` is the server's text.
 */
export class RSILearningFailedError extends RSIError {
  name = 'RSILearningFailedError';
  constructor(public executionId: string | undefined, public code: string, public detailMessage: string, public retryable?: boolean,
    public phase?: string, public upstreamStatus?: number, public upstreamCode?: string, public requestId?: string) {
    super(`Learning failed for execution ${executionId}: ${code}`);
  }

  /** Build the error from a failed learning view as returned by the service. */
  static fromLearning(learning: any, requestId?: string): RSILearningFailedError {
    const raw = learning?.error_detail;
    const detail = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    const code = detail.code || learning?.error || 'failed';
    return new RSILearningFailedError(learning?.execution_id, code, detail.message || code, detail.retryable ?? undefined,
      detail.phase ?? learning?.phase ?? undefined, detail.upstream_status ?? undefined, detail.upstream_code ?? undefined, requestId);
  }
}

const STOPPED: Record<string, string> = {
  interrupted: 'was interrupted; call resumeLearning to continue it',
  awaiting_runner: 'is waiting for a model; call resumeLearning with a model',
  awaiting_material: 'kept the current Recipe version{gap}; learning more needs feedback or evidence in a new execution with previousExecutionId',
};

/**
 * An execution's learning stopped in a state only the caller can change: `interrupted`,
 * `awaiting_runner` or `awaiting_material`. Not a failure; `RSILearningFailedError` is thrown for `failed`.
 */
export class RSILearningStoppedError extends RSIError {
  name = 'RSILearningStoppedError';
  readonly executionId: string | undefined;
  readonly status: string;
  constructor(public learning: any, public requestId?: string) {
    const gap = learning?.material_gap;
    super(`Learning for execution ${learning?.execution_id} ${(STOPPED[learning?.status] ?? 'stopped').replace('{gap}', gap ? ` (${gap})` : '')}`);
    this.executionId = learning?.execution_id;
    this.status = learning?.status;
  }
}
