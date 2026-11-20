const SERVICE_URL = 'https://rsi.cosens.ai/api/v1/memory';

export interface MemoryEndpointOptions { baseUrl?: string; apiKey?: string }

export interface MemoryIngest extends MemoryEndpointOptions {
  content: string;
  requestKey: string;
  sourceKind?: string;
  durable?: boolean;
  sourceId?: string;
  expectedRevision?: number;
  virtualUserId?: string;
}
export interface MemoryRecallResult extends Record<string, unknown> {
  items: Record<string, unknown>[];
}
export class MemoryError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code?: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = 'MemoryError';
  }
}

function boundedInteger(value: number, name: string, min: number, max: number) {
  if (!Number.isSafeInteger(value) || value < min || value > max)
    throw new TypeError(`${name} must be an integer from ${min} to ${max}`);
  return value;
}

function pathPart(id: string) {
  if (typeof id !== 'string' || !id || id === '.' || id === '..')
    throw new TypeError('resource ID must be a nonempty safe path segment');
  return encodeURIComponent(id);
}

export function memoryRecall({
  query,
  tokenBudget = 1000,
  virtualUserId,
  baseUrl, apiKey,
}: MemoryEndpointOptions & {
  query: string;
  tokenBudget?: number;
  virtualUserId?: string;
}) {
  return request<MemoryRecallResult>('POST', '/recall', {
    query,
    token_budget: boundedInteger(tokenBudget, 'tokenBudget', 1, 8000),
  }, virtualUserId, { baseUrl, apiKey });
}

export function memoryIngest({
  content,
  requestKey,
  sourceKind = 'decision',
  durable = true,
  sourceId,
  expectedRevision,
  virtualUserId,
  baseUrl, apiKey,
}: MemoryIngest) {
  if (
    typeof requestKey !== 'string' ||
    !requestKey.trim() ||
    requestKey.length > 256 ||
    Array.from(requestKey).some(
      (character) =>
        character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127,
    )
  )
    throw new TypeError(
      'requestKey must be 1–256 characters without control characters',
    );
  if (
    sourceId !== undefined &&
    (typeof sourceId !== 'string' || !sourceId.trim())
  )
    throw new TypeError('sourceId must be a nonempty string');
  return request('POST', '/ingest', {
    content,
    request_key: requestKey,
    source_kind: sourceKind,
    durable,
    ...(sourceId === undefined ? {} : { source_id: sourceId }),
    ...(expectedRevision === undefined
      ? {}
      : {
          expected_revision: boundedInteger(
            expectedRevision,
            'expectedRevision',
            0,
            Number.MAX_SAFE_INTEGER,
          ),
        }),
  }, virtualUserId, { baseUrl, apiKey });
}

export function getMemoryItems({ limit = 200, virtualUserId, baseUrl, apiKey }: MemoryEndpointOptions & { limit?: number; virtualUserId?: string } = {}) {
  return request(
    'GET',
    `/items?limit=${boundedInteger(limit, 'limit', 1, 500)}`,
    undefined, virtualUserId, { baseUrl, apiKey },
  );
}

export function getMemoryJob(jobId: string, { virtualUserId, baseUrl, apiKey }: MemoryEndpointOptions & { virtualUserId?: string } = {}) {
  return request('GET', `/jobs/${pathPart(jobId)}`, undefined, virtualUserId, { baseUrl, apiKey });
}

export function getMemoryExperience(
  experienceId: string,
  { revision, virtualUserId }: { revision?: number; virtualUserId?: string } = {},
) {
  if (virtualUserId !== undefined) throw new TypeError("Virtual User selection is not supported for experience details");
  const suffix =
    revision === undefined
      ? ''
      : `?${new URLSearchParams({ revision: String(revision) })}`;
  return request('GET', `/experiences/${pathPart(experienceId)}${suffix}`);
}

export function getMemory({ virtualUserId }: { virtualUserId?: string } = {}) {
  if (virtualUserId !== undefined) throw new TypeError("Virtual User selection is not supported for memory overview");
  return request('GET', '');
}

function serviceUrl(baseUrl: string): string {
  if (typeof baseUrl !== 'string' || /[\s\x00-\x1f\x7f\\]/.test(baseUrl))
    throw new TypeError('baseUrl must be an absolute HTTPS URL');
  const match = /^https:\/\/([^/?#]+)(\/[^?#]*)?$/i.exec(baseUrl);
  if (!match || match[1].includes('@')) throw new TypeError('baseUrl must be an absolute HTTPS URL without credentials, query or fragment');
  let parsed: URL;
  try { parsed = new URL(baseUrl); } catch { throw new TypeError('Invalid baseUrl'); }
  if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password)
    throw new TypeError('baseUrl must be an absolute HTTPS URL without credentials');
  for (const segment of (match[2] ?? '').split('/')) {
    let decoded = segment;
    try {
      for (let i = 0; i < 5; i++) {
        const next = decodeURIComponent(decoded);
        if (next === decoded) break;
        decoded = next;
      }
    } catch { throw new TypeError('Invalid baseUrl path'); }
    if (decoded === '.' || decoded === '..' || /[\/\\%\s\x00-\x1f\x7f]/.test(decoded))
      throw new TypeError('baseUrl path must not contain traversal or encoded separators');
  }
  return baseUrl.replace(/\/+$/, '');
}

async function request<T = Record<string, unknown>>(
  method: 'GET' | 'POST',
  path: string,
  body?: object,
  virtualUserId?: string,
  options: MemoryEndpointOptions = {},
): Promise<T> {
  if (virtualUserId !== undefined && (typeof virtualUserId !== "string" || !virtualUserId || /[\s\x00-\x1f\x7f]/.test(virtualUserId)))
    throw new TypeError("virtualUserId must be a nonempty header value");
  if ((options.baseUrl === undefined) !== (options.apiKey === undefined))
    throw new TypeError('baseUrl and apiKey must be supplied together');
  const base = options.baseUrl === undefined ? SERVICE_URL : serviceUrl(options.baseUrl);
  const apiKey = options.baseUrl === undefined ? process.env.COSENS_API_KEY : options.apiKey;
  if (typeof apiKey !== 'string' || !apiKey || /[\s\x00-\x1f\x7f]/.test(apiKey))
    throw new TypeError('Set COSENS_API_KEY to the bare CoSens API key');
  const headers = new Headers({
    'X-Auth-Type': 'api_key',
    Authorization: `Bearer ${apiKey}`,
  });
  if (body !== undefined) headers.set('Content-Type', 'application/json');
  if (options.baseUrl !== undefined) headers.set('X-CoSens-Auth-Type', 'api_key');
  if (virtualUserId !== undefined) headers.set('X-Virtual-User-Id', virtualUserId);
  const redact = (value: string) => value.split(apiKey).join('[REDACTED]');
  let response: Response;
  try {
    response = await fetch(base + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      redirect: 'manual',
      signal: AbortSignal.timeout(30_000),
    });
  } catch {
    throw new MemoryError('CoSens Memory request failed');
  }
  const header = response.headers.get('x-request-id');
  const requestId = header ? redact(header) : undefined;
  if (response.status >= 300 && response.status < 400)
    throw new MemoryError(
      'CoSens gateway redirects are not followed',
      response.status,
      undefined,
      requestId,
    );

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    const message = response.ok
      ? 'CoSens Memory returned invalid JSON'
      : response.statusText;
    throw new MemoryError(
      redact(message),
      response.status,
      undefined,
      requestId,
    );
  }
  if (!response.ok) {
    const errorPayload = payload as {
      error?: { message?: string; code?: string };
      detail?: string | { message?: string; code?: string };
    } | null;
    const detail = errorPayload?.detail;
    const message =
      errorPayload?.error?.message ??
      (typeof detail === 'string' ? detail : detail?.message) ??
      response.statusText;
    const code =
      errorPayload?.error?.code ??
      (typeof detail === 'string' ? undefined : detail?.code);
    throw new MemoryError(
      redact(String(message)),
      response.status,
      code == null ? undefined : redact(String(code)),
      requestId,
    );
  }
  if (
    payload &&
    typeof payload === 'object' &&
    !Array.isArray(payload) &&
    !Object.hasOwn(payload, 'requestId')
  )
    Object.defineProperty(payload, 'requestId', {
      value: requestId,
      enumerable: false,
    });
  return payload as T;
}
