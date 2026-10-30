/** Document metadata and one-time upload URL validation. Never executes grant instructions. */
export interface DocumentUploadMetadata {
  filename: string; mimeType: string; threadId?: string; uploadedAt?: string; sourceKind?: string;
  workspaceId?: string; ingestMode?: string; summary?: string; background?: string;
  waitForProcessing?: boolean; waitTimeoutSeconds?: number;
}
export interface DocumentUploadGrant {
  upload_url: string; method: string; status?: string; ok?: boolean; expires_at?: string; max_bytes?: number;
  [key: string]: unknown;
}

const metadataFields: Record<string, string> = {
  filename: 'filename', mimeType: 'mime_type', threadId: 'thread_id', uploadedAt: 'uploaded_at',
  sourceKind: 'source_kind', workspaceId: 'workspace_id', ingestMode: 'ingest_mode', summary: 'summary',
  background: 'background', waitForProcessing: 'wait_for_processing', waitTimeoutSeconds: 'wait_timeout_seconds',
};

export function documentMetadata(options: DocumentUploadMetadata): Record<string, string | number | boolean> {
  for (const name of ['filename', 'mimeType'] as const) {
    const value = options[name];
    if (typeof value !== 'string' || !value.trim() || /[\r\n\x00]/.test(value)) throw new TypeError(`document ${name} must be nonempty text without control characters`);
  }
  const body: Record<string, string | number | boolean> = {};
  for (const [name, value] of Object.entries(options)) {
    if (!Object.hasOwn(metadataFields, name)) throw new TypeError('unsupported document metadata; user_id and graph_id are gateway-owned');
    if (value === undefined || value === null) continue;
    if (name === 'waitForProcessing') {
      if (typeof value !== 'boolean') throw new TypeError('waitForProcessing must be a boolean');
    } else if (name === 'waitTimeoutSeconds') {
      if (!Number.isInteger(value) || value < 1 || value > 3600) throw new TypeError('waitTimeoutSeconds must be an integer from 1 to 3600');
    } else if (typeof value !== 'string') throw new TypeError(`document ${name} must be text`);
    body[metadataFields[name]!] = value;
  }
  return body;
}

export function documentBytes(content: Uint8Array): Uint8Array<ArrayBuffer> {
  if (!(content instanceof Uint8Array)) throw new TypeError('document content must be bytes');
  if (!content.byteLength) throw new TypeError('document upload content must not be empty');
  return Uint8Array.from(content);
}

function expiryTimestamp(value: unknown): number {
  if (typeof value !== 'string') return Number.NaN;
  const parts = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!parts) return Number.NaN;
  const [year, month, day, hour, minute, second] = parts.slice(1).map(Number) as [number, number, number, number, number, number];
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  // Date.parse rolls invalid dates such as February 30 into the next month.
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1]! || hour > 23 || minute > 59 || second > 59) return Number.NaN;
  return Date.parse(value);
}

export function uploadTarget(grant: DocumentUploadGrant, content: Uint8Array, serviceUrl: string) {
  if (!grant || grant.method !== 'PUT' || grant.ok === false) throw new TypeError('upload grant must authorize PUT');
  if (grant.status !== undefined && grant.status !== 'awaiting_upload') throw new TypeError('upload grant is not awaiting_upload');
  const url = grant.upload_url;
  if (typeof url !== 'string' || !url || /[\s\x00-\x1f\\?#]/.test(url)) throw new TypeError('invalid document upload target');
  const service = new URL(serviceUrl);
  let path = url;
  if (url.startsWith('https://')) {
    let target: URL;
    try { target = new URL(url); } catch { throw new TypeError('invalid document upload target'); }
    const authorityEnd = url.indexOf('/', 'https://'.length);
    if (target.origin !== service.origin || url.slice('https://'.length, authorityEnd).includes('@')) throw new TypeError('invalid document upload target');
    // Validate the original path before URL normalization can remove dot segments.
    path = url.slice(url.indexOf('/', 'https://'.length));
    if (!path.startsWith(service.pathname + '/')) throw new TypeError('invalid document upload target');
    path = path.slice(service.pathname.length);
  } else if (path.startsWith(service.pathname + '/')) path = path.slice(service.pathname.length);
  const matched = /^\/v1\/document\/uploads\/([A-Za-z0-9_-]+)$/.exec(path);
  if (!matched) throw new TypeError('invalid document upload target');
  if (grant.expires_at !== undefined) {
    const expires = expiryTimestamp(grant.expires_at);
    if (!Number.isFinite(expires) || expires <= Date.now()) {
      throw new TypeError('document upload grant has expired or has an invalid expiry');
    }
  }
  const bytes = documentBytes(content);
  if (grant.max_bytes !== undefined && (!Number.isSafeInteger(grant.max_bytes) || grant.max_bytes < 1 || bytes.byteLength > grant.max_bytes)) {
    throw new TypeError('document upload exceeds max_bytes or grant limit is invalid');
  }
  return { path, token: matched[1]!, bytes };
}

export function documentStatusBody(documentIds: string[]) {
  if (!Array.isArray(documentIds) || !documentIds.length || documentIds.some(id => typeof id !== 'string' || !id.trim())) {
    throw new TypeError('documentIds must be a nonempty list of document IDs');
  }
  return { document_ids: documentIds };
}
