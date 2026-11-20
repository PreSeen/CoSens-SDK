import { createHash } from 'node:crypto';
import { fail } from './models.js';

const prefix = 'RSI_MODEL_HEADERS_';

export function bindModelHeaders(
  baseUrl: string,
  model: string,
  headers: Record<string, string>,
) {
  let entries: [string, string][];
  try {
    entries = [...new Headers(headers).entries()];
  } catch {
    return fail('invalid_argument', 'Invalid model request headers');
  }
  // Pin work to the original model endpoint and header values across processes.
  const identity = JSON.stringify([baseUrl.replace(/\/+$/, ''), model, entries]);
  const name = prefix + createHash('sha256').update(identity).digest('hex');
  return { name, value: JSON.stringify(Object.fromEntries(entries)) };
}

export function isHeaderBinding(name: string) {
  return name.startsWith(prefix);
}

export function modelHeaders(name: string): Record<string, string> {
  const value = process.env[name];
  if (!value) return fail('invalid_state', 'Model headers are unavailable for this execution');
  return isHeaderBinding(name)
    ? JSON.parse(value)
    : { authorization: `Bearer ${value}` };
}
