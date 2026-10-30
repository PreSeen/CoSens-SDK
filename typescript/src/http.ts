import { resolveApiKey, resolveService, type Service } from './config.js';
import { RSIHTTPError, RSITransportError } from './errors.js';

export interface TransportOptions { apiKey?: string; region?: string; fetch?: typeof fetch; timeoutMs?: number }

export class Transport {
  readonly service: Service;
  readonly apiKey: string;
  private readonly fetcher: typeof fetch;
  private readonly timeoutMs: number;
  constructor(options: TransportOptions = {}) {
    this.service = resolveService(options.region);
    this.apiKey = resolveApiKey(options.apiKey, this.service.region);
    this.fetcher = options.fetch ?? fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
  }
  async request(method: string, path: string, body?: unknown, key?: string, sensitiveValues: string[] = []): Promise<any> {
    return this.send(method, path, body === undefined ? undefined : JSON.stringify(body), key, body === undefined ? undefined : 'application/json', sensitiveValues);
  }
  async requestBytes(method: string, path: string, content: Uint8Array, sensitiveValues: string[] = []): Promise<any> {
    return this.send(method, path, Uint8Array.from(content).buffer, undefined, 'application/octet-stream', sensitiveValues);
  }
  async requestMultipart(method: string, path: string, filename: string, mimeType: string, content: Uint8Array,
    fields: Record<string, string | number | boolean>): Promise<any> {
    const body = new FormData();
    for (const [name, value] of Object.entries(fields)) body.append(name, String(value));
    body.append('file', new Blob([Uint8Array.from(content)], { type: mimeType }), filename);
    return this.send(method, path, body);
  }
  private async send(method: string, path: string, body?: BodyInit, key?: string, contentType?: string, sensitiveValues: string[] = []): Promise<any> {
    if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\') || /^\/api(?:[/?#]|$)/.test(path)) {
      throw new TypeError('CoSens requires a service-relative path without /api');
    }
    const url = new URL(this.service.url + path);
    if (url.origin !== new URL(this.service.url).origin || !url.pathname.startsWith('/api/')) {
      throw new TypeError('CoSens requires a service-relative path');
    }
    const headers = new Headers({ 'X-Auth-Type': 'api_key', Authorization: `Bearer ${this.apiKey}` });
    if (key !== undefined) headers.set('Idempotency-Key', key);
    if (contentType !== undefined) headers.set('Content-Type', contentType);
    const redact = (value: unknown) => [this.apiKey, ...sensitiveValues].reduce((text, secret) => secret ? text.split(secret).join('[REDACTED]') : text, String(value));
    let response: Response;
    try {
      response = await this.fetcher(url.toString(), {
        method, headers, body,
        redirect: 'manual', signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (error) {
      throw new RSITransportError(`CoSens transport failed: ${error instanceof Error ? redact(error.name) : 'unknown error'}`);
    }
    const requestId = response.headers.get('x-request-id') ? redact(response.headers.get('x-request-id')) : undefined;
    if (response.status >= 300 && response.status < 400) throw new RSIHTTPError(response.status, 'CoSens gateway redirects are not followed', undefined, requestId);
    let payload: any;
    try { payload = await response.json(); }
    catch (error) {
      if (!response.ok) throw new RSIHTTPError(response.status, redact(response.statusText), undefined, requestId);
      if (error instanceof SyntaxError) throw new RSITransportError(`CoSens returned invalid JSON (HTTP ${response.status})`);
      throw new RSITransportError(`CoSens response read failed (HTTP ${response.status})`);
    }
    if (!response.ok) {
      const error = typeof payload?.error === 'object' ? payload.error : undefined;
      const detail = typeof payload?.detail === 'object' && payload.detail !== null ? payload.detail : undefined;
      const code = error?.code ?? detail?.code;
      throw new RSIHTTPError(response.status, redact(error?.message ?? detail?.message ?? payload?.detail ?? response.statusText), code == null ? undefined : redact(code), requestId);
    }
    return payload;
  }
}

export function pathPart(id: string): string {
  if (typeof id !== 'string' || !id || id === '.' || id === '..') throw new TypeError('resource ID must be a nonempty safe path segment');
  return encodeURIComponent(id);
}
export function requestKey(key: string): string {
  if (typeof key !== 'string' || !key.trim() || key.length > 256 || /[\x00-\x1f\x7f]/.test(key)) throw new TypeError('key must be 1–256 characters without control characters');
  return key;
}
