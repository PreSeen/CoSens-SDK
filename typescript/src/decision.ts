import { structuredResponse } from './structured.js';
import { RSIDecisionUnavailable } from './errors.js';

export const DEFAULT_DECISION_TIMEOUT_MS = 5000;
export const DEFAULT_DECISION_MAX_OUTPUT_TOKENS = 64;

export interface Decision { activate: boolean; objective: string; task_family: string; reason: string; capture?: string }
const FORMAT = { type: 'json_schema', name: 'cosens_decision', strict: true, schema: { type: 'object', properties: { activate: { type: 'boolean' }, objective: { type: 'string' }, task_family: { type: 'string' }, reason: { type: 'string' } }, required: ['activate', 'objective', 'task_family', 'reason'], additionalProperties: false } };
/** One unretried policy call through `native` (the decision client), bounded by `timeoutMs` and `maxOutputTokens`. */
export async function decide(native: any, model: string, user: string, tools: string[], timeoutMs: number, maxOutputTokens: number, extraBody?: Record<string, unknown>): Promise<Decision> {
  const input = `Decide whether this user request benefits from recording this task execution so an explicit request can later improve Recipe/Memory. Return only the requested JSON fields. Do not treat this decision as authorization. Keep objective under 15 words, task_family to one or two words and reason under 10 words.\nRecent user text: ${user.slice(-8000)}\nTool names: ${tools.join(', ')}`;
  const response = await structuredResponse(native,{ model, input, text: { format: FORMAT }, max_output_tokens: maxOutputTokens, extra_body: extraBody, store: false }, { timeout: timeoutMs, maxRetries: 0 });
  try {
    const parsed = JSON.parse(response.output_text);
    const fields = ['activate', 'objective', 'task_family', 'reason'];
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) ||
      Object.keys(parsed).length !== fields.length || !fields.every(field => Object.hasOwn(parsed, field)) ||
      typeof parsed.activate !== 'boolean' || typeof parsed.objective !== 'string' ||
      typeof parsed.task_family !== 'string' || typeof parsed.reason !== 'string') throw Error('invalid');
    return parsed;
  } catch { throw new RSIDecisionUnavailable('Decision model returned invalid structured output'); }
}
