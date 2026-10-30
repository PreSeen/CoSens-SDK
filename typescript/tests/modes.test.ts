import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cosens, RSIApprovalUnavailable } from '../src/index.ts';

const statePath = () => join(mkdtempSync(join(tmpdir(), 'cosens-modes-')), 'state.sqlite3');
const completed = { id: 'response-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };

test('auto decision failure still recalls and executes one business request without a Run', async () => {
  const calls: string[] = [];
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) { calls.push('decision'); throw Error('model unavailable'); }
    calls.push('business'); return completed;
  } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname; calls.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const result = await client.responses.create({ model: 'model', input: 'hello' });
  assert.equal(result, completed);
  assert.deepEqual(calls, ['decision', '/api/v1/recipes/active', '/api/v1/memory/recall', 'business']);
  assert.equal(client.cosens.rsiCapture, 'decision_failed');
  await client.close();
});

test('auto decision false uses its task family for independent recall without creating a Run', async () => {
  const calls: string[] = [];
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) { calls.push('decision'); return { output_text: JSON.stringify({ activate: false, objective: 'hello', task_family: 'research', reason: 'not useful' }) }; }
    calls.push('business'); return completed;
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input)); calls.push(url.pathname + url.search);
    if (url.pathname.endsWith('/memory/recall')) assert.deepEqual(JSON.parse(String(init?.body)), { query: 'hello', token_budget: 1000 });
    return new Response(JSON.stringify(url.pathname.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  assert.equal(await client.responses.create({ model: 'model', input: 'hello' }), completed);
  assert.deepEqual(calls, ['decision', '/api/v1/recipes/active?task_family=research', '/api/v1/memory/recall', 'business']);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.runId, null);
  await client.close();
});

test('approval activation stops before gateway writes and the business model', async () => {
  let businessCalls = 0, gatewayCalls = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    businessCalls++; return completed;
  } } };
  const client = cosens(native, { apiKey: 'key', mode: 'approval', statePath: statePath(), fetch: async () => { gatewayCalls++; throw Error('not allowed'); } });
  await assert.rejects(client.responses.create({ model: 'model', input: 'hello' }), RSIApprovalUnavailable);
  assert.equal(gatewayCalls, 0);
  assert.equal(businessCalls, 0);
  await client.close();
});

test('malformed standalone Memory response blocks native model execution', async () => {
  let businessCalls = 0;
  const native = { responses: { create: async () => { businessCalls++; return completed; } } };
  const fetcher: typeof fetch = async input => new Response(JSON.stringify(new URL(String(input)).pathname.endsWith('/recipes/active')
    ? { status: 'matched', recipe_id: 'recipe-1', bundle: {} } : { unexpected: true }));
  const client = cosens(native, { apiKey: 'key', mode: 'disable', statePath: statePath(), fetch: fetcher });
  await assert.rejects(client.responses.create({ model: 'model', input: 'hello' }), /Memory recall/);
  assert.equal(businessCalls, 0);
  await client.close();
});
