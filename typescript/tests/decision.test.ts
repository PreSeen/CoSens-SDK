import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decide } from '../src/decision.ts';
import { RSIDecisionUnavailable } from '../src/errors.ts';
import { cosens } from '../src/index.ts';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// A Chat Completions result carrying a structured decision.
function chatContent(value: unknown) {
  return { choices: [{ message: { content: JSON.stringify(value) } }] };
}

test('auto wrapper defaults to a 60 second decision timeout', async () => {
  let timeout: number | undefined;
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async (_body: any, options?: any) => {
      timeout = options?.timeout;
      return chatContent({ activate: false, objective: 'task', task_family: 'general', reason: 'routine' });
    } } },
  };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    const value = path.endsWith('/recipe-candidates') ? { items: [], has_more: false } : { items: [] };
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  };
  const wrapped = cosens(native, { apiKey: 'test-key', mode: 'auto', fetch: fetcher,
    statePath: join(mkdtempSync(join(tmpdir(), 'cosens-timeout-')), 'state.sqlite') });
  try {
    await wrapped.responses.create({ model: 'model', input: 'task' });
    assert.equal(timeout, 60000);
  } finally { await wrapped.close(); }
});

test('auto decision failure still returns the native response', async () => {
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async () => { throw new Error('decision timed out'); } } },
  };
  const wrapped = cosens(native, { apiKey: 'test-key', mode: 'auto',
    fetch: async () => { throw new Error('read-only lookup should be skipped'); },
    statePath: join(mkdtempSync(join(tmpdir(), 'cosens-fallback-')), 'state.sqlite') });
  try {
    const response = await wrapped.responses.create({ model: 'model', input: 'task' });
    assert.equal(response.output_text, 'answer');
    assert.equal(wrapped.cosens.rsiCapture, 'decision_failed');
  } finally { await wrapped.close(); }
});

test('auto recipe match timeout returns the native response', async () => {
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async (body: any) => {
      if (body.response_format.json_schema.name === 'cosens_recipe_match') throw new Error('match timed out');
      return chatContent({ activate: true, objective: 'task', task_family: 'general', reason: 'task' });
    } } },
  };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    const value = path.endsWith('/recipe-candidates')
      ? { items: [{ recipe_candidate_id: 'candidate', recipe_id: 'recipe', version: 1,
          status: 'candidate', current: true, recipe: { title: 'Test', steps: [] } }], has_more: false }
      : { items: [] };
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  };
  const wrapped = cosens(native, { apiKey: 'test-key', mode: 'auto', fetch: fetcher,
    statePath: join(mkdtempSync(join(tmpdir(), 'cosens-match-fallback-')), 'state.sqlite') });
  try {
    const response = await wrapped.responses.create({ model: 'model', input: 'task' });
    assert.equal(response.output_text, 'answer');
    assert.equal(wrapped.cosens.rsiCapture, 'policy_failed');
    assert.equal(wrapped.cosens.getTurn(wrapped.cosens.lastTurnId)?.revisionId, undefined);
  } finally { await wrapped.close(); }
});

test('decision timeout stays in OpenAI request options, outside the model JSON body', async () => {
  let body: any;
  let options: any;
  const native = { chat: { completions: { create: async (request: any, requestOptions: any) => {
    body = request; options = requestOptions;
    return chatContent({ activate: false, objective: 'task', task_family: 'general', reason: 'no run' });
  } } } };
  await decide(native, 'decision-model', 'task', ['search'], 5000, 256);
  assert.equal(body.timeout, undefined);
  assert.equal(options.timeout, 5000);
  assert.equal(body.max_completion_tokens, 256);
  assert.equal(body.response_format.json_schema.name, 'cosens_decision');
});

test('decision parser rejects missing reason and extra model fields', async () => {
  for (const value of [
    { activate: true, objective: 'task', task_family: 'general' },
    { activate: true, objective: 'task', task_family: 'general', reason: 'yes', unauthorized: true },
  ]) {
    const native = { chat: { completions: { create: async () => chatContent(value) } } };
    await assert.rejects(decide(native, 'decision-model', 'task', [], 5000, 256), RSIDecisionUnavailable);
  }
});
