import { DEFAULT_MAX_CONTEXT_CHARS, renderGuidance } from './render.js';


export function userText(path: string[], params: any): string {
  const value = path[0] === 'responses' ? params.input : params.messages;
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return '';
  const texts: string[] = [];
  let item: any;
  for (let index = value.length - 1; index >= 0; index--) if (value[index]?.role === 'user') { item = value[index]; break; }
  if (typeof item?.content === 'string') texts.push(item.content);
  else if (Array.isArray(item?.content)) for (const part of item.content) if ((part?.type === 'text' || part?.type === 'input_text') && typeof part.text === 'string') texts.push(part.text);
  return texts.join('\n').slice(-8000);
}
export function toolNames(params: any): string[] { return (params.tools ?? []).map((x: any) => x?.name ?? x?.function?.name).filter((x: any) => typeof x === 'string'); }
/** Append the frozen Recipe/Memory context as compact text, or return the request unchanged. */
export function inject(path: string[], params: any, context: any, maxChars = DEFAULT_MAX_CONTEXT_CHARS): any {
  const text = renderGuidance(context?.recipe?.status === 'matched' ? context.recipe.bundle : undefined, context?.snapshot?.evidence, maxChars);
  if (text === undefined) return params;
  const block = `\n\n${text}`;
  if (path[0] === 'responses') {
    if (params.instructions !== undefined && typeof params.instructions !== 'string') throw new TypeError('Cannot safely append CoSens context to non-text Responses instructions');
    return { ...params, instructions: (params.instructions ?? '') + block };
  }
  if (path[0] === 'chat' && path[1] === 'completions') {
    if (!Array.isArray(params.messages)) throw new TypeError('Cannot safely append CoSens context to non-list chat messages');
    return { ...params, messages: [...params.messages, { role: 'developer', content: block }] };
  }
  throw new TypeError('Unsupported generation method');
}
export function terminal(response: any): boolean {
  if (response?.status && response.status !== 'completed') return false;
  if (Array.isArray(response?.output)) return response.output.some((x: any) => x?.type === 'message') && !response.output.some((x: any) => x?.type === 'function_call');
  if (Array.isArray(response?.choices)) return response.choices.length > 0 && response.choices.every((x: any) => x.finish_reason && !['tool_calls', 'function_call'].includes(x.finish_reason));
  return !!response?.output_text;
}
export function toolContinuation(response: any): boolean {
  if (response?.status && response.status !== 'completed') return false;
  if (Array.isArray(response?.output)) return response.output.some((item: any) => item?.type === 'function_call');
  if (Array.isArray(response?.choices)) return response.choices.some((choice: any) => ['tool_calls', 'function_call'].includes(choice?.finish_reason));
  return false;
}
export function summary(response: any): string {
  return String(response?.output_text ?? response?.choices?.[0]?.message?.content ?? response?.choices?.[0]?.text ?? '');
}
