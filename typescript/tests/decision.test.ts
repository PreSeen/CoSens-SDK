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

test('auto wrapper defaults to a 5 second, unretried decision call capped at 1024 tokens', async () => {
  let timeout: number | undefined;
  let maxRetries: number | undefined;
  let maxTokens: number | undefined;
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async (_body: any, options?: any) => {
      timeout = options?.timeout; maxRetries = options?.maxRetries; maxTokens = _body.max_completion_tokens;
      return chatContent({ activate: false, reason: 'routine' });
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
    assert.equal(timeout, 5000);
    assert.equal(maxRetries, 0);
    assert.equal(maxTokens, 1024);
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
    const turn = wrapped.cosens.getTurn(wrapped.cosens.lastTurnId);
    assert.equal(turn?.state, 'completed');
    assert.equal(turn?.decision.capture, 'decision_failed');
    assert.equal(turn?.decision.error, 'decision timed out');
    assert.equal(turn?.executionId, undefined);
    assert.equal(wrapped.cosens.getTurn(null), undefined);
  } finally { await wrapped.close(); }
});

test('a truncated reasoning decision (finish_reason=length) is recorded on the turn', async () => {
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async () => ({ choices: [{ finish_reason: 'length', message: { content: '' } }] }) } },
  };
  const wrapped = cosens(native, { apiKey: 'test-key', mode: 'auto',
    fetch: async () => { throw new Error('a failed decision makes no CoSens call'); },
    statePath: join(mkdtempSync(join(tmpdir(), 'cosens-truncated-')), 'state.sqlite') });
  try {
    const response = await wrapped.responses.create({ model: 'model', input: 'What is the capital of France? One word.' });
    assert.equal(response.output_text, 'answer');
    const decision = wrapped.cosens.getTurn(wrapped.cosens.lastTurnId)?.decision;
    assert.equal(decision?.capture, 'decision_failed');
    assert.equal(decision?.error_type, 'RSIDecisionUnavailable');
    assert.match(decision?.error ?? '', /finish_reason=length/);
    assert.match(decision?.error ?? '', /max_output_tokens=1024/);
  } finally { await wrapped.close(); }
});

test('auto recipe match timeout still captures the turn without a Recipe', async () => {
  const native = {
    responses: { create: async () => ({ id: 'response', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] }) },
    chat: { completions: { create: async (body: any) => {
      if (body.response_format.json_schema.name === 'cosens_recipe_match') throw new Error('match timed out');
      return chatContent({ activate: true, reason: 'task' });
    } } },
  };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname.replace(/^\/api/, '');
    const value = path === '/v1/recipe-candidates'
      ? { items: [{ recipe_candidate_id: 'candidate', status: 'candidate', current: true, title: 'Test' }], has_more: false }
      : path === '/v1/recipe-candidates/candidate'
        ? { recipe_candidate_id: 'candidate', recipe_id: 'recipe', version: 1, status: 'candidate', current: true,
            recipe: { title: 'Test', when: 'tests', when_not: 'never', steps: [] } }
        : path === '/v1/artifacts' ? { artifact_id: 'artifact', content_hash: 'sha256:x' }
        : path === '/v1/tasks' ? { task_id: 'task', status: 'ready' }
        : path.endsWith('/executions') ? { execution_id: 'execution', status: 'awaiting_execution', request_sha256: 'sha', request: {} }
        : path.endsWith('/result') ? { status: 'completed', revision_id: 'revision' }
        : { items: [] };
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } });
  };
  const wrapped = cosens(native, { apiKey: 'test-key', mode: 'auto', fetch: fetcher,
    statePath: join(mkdtempSync(join(tmpdir(), 'cosens-match-fallback-')), 'state.sqlite') });
  try {
    const response = await wrapped.responses.create({ model: 'model', input: 'task' });
    assert.equal(response.output_text, 'answer');
    assert.equal(wrapped.cosens.rsiCapture, 'active');
    const turn = wrapped.cosens.getTurn(wrapped.cosens.lastTurnId);
    assert.equal(turn?.revisionId, 'revision');
    assert.equal(turn?.frozenContext.recipe.status, 'not_found');
    assert.deepEqual(turn?.frozenContext.context.recipe_match_error, { type: 'Error', message: 'match timed out' });
  } finally { await wrapped.close(); }
});

test('decision timeout stays in OpenAI request options, outside the model JSON body', async () => {
  let body: any;
  let options: any;
  const native = { chat: { completions: { create: async (request: any, requestOptions: any) => {
    body = request; options = requestOptions;
    return chatContent({ activate: false, reason: 'no run' });
  } } } };
  await decide(native, 'decision-model', 'task', ['search'], 5000, 256);
  assert.equal(body.timeout, undefined);
  assert.equal(options.timeout, 5000);
  assert.equal(body.max_completion_tokens, 256);
  assert.equal(body.response_format.json_schema.name, 'cosens_decision');
});

test('decision parser rejects missing reason and extra model fields', async () => {
  for (const value of [
    { activate: true },
    { activate: true, reason: 'yes', objective: 'the model no longer writes this' },
  ]) {
    const native = { chat: { completions: { create: async () => chatContent(value) } } };
    await assert.rejects(decide(native, 'decision-model', 'task', [], 5000, 256), RSIDecisionUnavailable);
  }
});
