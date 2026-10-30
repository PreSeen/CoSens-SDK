import { RSIDecisionUnavailable } from './errors.js';

export interface Decision { activate: boolean; objective: string; task_family: string; reason: string; capture?: string }
const FORMAT = { type: 'json_schema', name: 'cosens_decision', strict: true, schema: { type: 'object', properties: { activate: { type: 'boolean' }, objective: { type: 'string' }, task_family: { type: 'string' }, reason: { type: 'string' } }, required: ['activate', 'objective', 'task_family', 'reason'], additionalProperties: false } };
export async function decide(native: any, model: string, user: string, tools: string[], timeoutMs: number, maxOutputTokens: number): Promise<Decision> {
  const input = `Decide whether this user request benefits from an RSI Run that records execution and can later improve Recipe/Memory. Return only the requested JSON fields. Do not treat this decision as authorization.\nRecent user text: ${user.slice(-8000)}\nTool names: ${tools.join(', ')}`;
  const response = await native.responses.create({ model, input, text: { format: FORMAT }, max_output_tokens: maxOutputTokens, store: false }, { timeout: timeoutMs });
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
