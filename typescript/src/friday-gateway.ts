import OpenAI from 'openai';

export const FRIDAY_US_BASE_URL = 'https://llm.cosens.ai/v1';

/** Use the user's Friday credential. Query client.models.list() for public model IDs. */
export function fridayGatewayClient(credential: string, authType: 'api_key' | 'access_token' = 'api_key'): OpenAI {
  if (typeof credential !== 'string' || !credential || /\s/.test(credential)) throw new TypeError('Friday credential must be a nonempty bare token');
  if (authType !== 'api_key' && authType !== 'access_token') throw new TypeError('invalid Friday authentication type');
  return new OpenAI({ apiKey: credential, baseURL: FRIDAY_US_BASE_URL,
    defaultHeaders: { 'X-Friday-Auth-Type': authType }, maxRetries: 0 });
}
