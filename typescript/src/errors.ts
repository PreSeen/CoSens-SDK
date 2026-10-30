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
