/** Explicit provider connection for a server-side improvement job. */
export interface BusinessModelAPI {
  baseUrl: string;
  model: string;
  apiKey?: string;
  temperature?: number;
  maxTokens?: number;
  fridayAuthType?: 'api_key' | 'access_token';
}

export function modelAPIData(api: BusinessModelAPI): { wire: Record<string, unknown>; secrets: string[] } {
  if (!api || typeof api !== 'object' || Array.isArray(api) || Object.keys(api).some(k => !['baseUrl', 'model', 'apiKey', 'temperature', 'maxTokens', 'fridayAuthType'].includes(k))) {
    throw new TypeError('modelApi requires baseUrl, model and optional apiKey, temperature, maxTokens');
  }
  if (typeof api.model !== 'string' || !api.model.trim() || api.model.length > 256 || typeof api.baseUrl !== 'string') throw new TypeError('modelApi requires baseUrl and model');
  let url: URL;
  try { url = new URL(api.baseUrl); } catch { throw new TypeError('invalid modelApi.baseUrl'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || api.baseUrl.length > 2048) throw new TypeError('invalid modelApi.baseUrl');
  if (api.apiKey !== undefined && (typeof api.apiKey !== 'string' || /[\r\n]/.test(api.apiKey) || api.apiKey.length > 8192)) throw new TypeError('invalid modelApi.apiKey');
  const gatewayUrl = api.baseUrl.trim().replace(/\/+$/, '').replace(/\/chat\/completions$/, '');
  if (api.fridayAuthType !== undefined && (!['api_key', 'access_token'].includes(api.fridayAuthType) || !api.apiKey?.trim() || gatewayUrl !== 'https://llm.cosens.ai/v1')) throw new TypeError('Friday authentication requires a credential and the US gateway');
  if (gatewayUrl === 'https://llm.cosens.ai/v1' && api.fridayAuthType === undefined) throw new TypeError('Friday gateway requires an explicit fridayAuthType');
  if (api.temperature !== undefined && (!Number.isFinite(api.temperature) || api.temperature < 0 || api.temperature > 2)) throw new TypeError('invalid modelApi.temperature');
  if (api.maxTokens !== undefined && (!Number.isInteger(api.maxTokens) || api.maxTokens < 1 || api.maxTokens > 131072)) throw new TypeError('invalid modelApi.maxTokens');
  const wire: Record<string, unknown> = { base_url: api.baseUrl, model: api.model };
  if (api.apiKey !== undefined) wire.api_key = api.apiKey;
  if (api.temperature !== undefined) wire.temperature = api.temperature;
  if (api.maxTokens !== undefined) wire.max_tokens = api.maxTokens;
  if (api.fridayAuthType !== undefined) wire.friday_auth_type = api.fridayAuthType;
  return { wire, secrets: api.apiKey ? [api.apiKey] : [] };
}
