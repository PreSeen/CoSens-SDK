import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensClient, cosens } from '../src/index.ts';
import { ServiceFake } from './service-fake.ts';

const statePath = () => join(mkdtempSync(join(tmpdir(), 'recipe-budget-')), 'state.sqlite');

test('atomic learn and resume carry budgets and omit them when unset', async () => {
  const bodies: any[] = [];
  const c = new CoSensClient({ apiKey: 'key', fetch: async (_input, init) => {
    bodies.push(typeof init?.body === 'string' ? JSON.parse(init.body) : undefined);
    return new Response(JSON.stringify({ execution_id: 'execution', status: 'queued' }), { headers: { 'Content-Type': 'application/json' } });
  } });
  await c.atoms.learn('execution', { maxCompletionTokens: 200_000 });
  await c.atoms.resumeLearning('execution', { model: 'model', maxCompletionTokens: 300_000 });
  await c.atoms.learn('execution');
  assert.deepEqual(bodies, [{ max_completion_tokens: 200_000 }, { model: 'model', max_completion_tokens: 300_000 }, {}]);
});

test('invalid budgets never reach HTTP', async () => {
  const service = new ServiceFake(); const c = new CoSensClient({ apiKey: 'key', fetch: service.fetch });
  for (const maxCompletionTokens of [0, -1, 1.2, NaN, Infinity, 1_000_001, true, '200000']) {
    assert.throws(() => c.atoms.learn('execution', { maxCompletionTokens: maxCompletionTokens as number }), /maxCompletionTokens/);
    assert.throws(() => c.atoms.resumeLearning('execution', { maxCompletionTokens: maxCompletionTokens as number }), /maxCompletionTokens/);
  }
  assert.equal(service.requests.length, 0);
});

test('auto generateRecipe forwards its budget separately from business output tokens', async () => {
  const service = new ServiceFake();
  const business: any[] = [];
  const native = { responses: { create: async (params: any) => { business.push(params); return { output_text: 'final output', output: [{ type: 'message' }] }; } },
    chat: { completions: { create: async () => ({ choices: [{ message: { content: JSON.stringify({ activate: true, reason: 'task' }) } }] }) } } };
  const wrapped = cosens(native, { apiKey: 'key', mode: 'auto', fetch: service.fetch, statePath: statePath() });
  try {
    await wrapped.responses.create({ model: 'model', input: 'Produce output', max_output_tokens: 4096 });
    await wrapped.cosens.generateRecipe({ maxCompletionTokens: 200_000 });
    assert.deepEqual(service.requests.filter(r => r.path.endsWith('/learn')).map(r => r.body), [{ max_completion_tokens: 200_000 }]);
    assert.equal(business[0].max_output_tokens, 4096);
    await assert.rejects(wrapped.cosens.generateRecipe({ maxCompletionTokens: 300_000 }), (error: any) => error.statusCode === 422);
    assert.equal(service.generations, 1);
  } finally { await wrapped.close(); }
});
