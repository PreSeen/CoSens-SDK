import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient } from '../src/index.ts';

test('v3 public atoms send source revisions and guided steps through the gateway', async () => {
  const seen: Array<{ method: string; path: string; body: unknown; headers: Headers }> = [];
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    const url = new URL(String(input));
    seen.push({ method: init?.method ?? '', path: url.pathname + url.search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined, headers: new Headers(init?.headers) });
    return new Response(JSON.stringify({ status: 'ok' }));
  } });
  await client.atoms.memoryIngest({ content: 'updated', requestKey: 'source-2', sourceId: 's1', expectedRevision: 2, sourceKind: 'message', durable: false });
  await client.atoms.getMemoryItems({ limit: 500 });
  await client.atoms.nextStep('run-1');
  await client.atoms.submitStep('run-1', 'attempt-1', { attemptToken: 'opaque-token', outputs: { report: 'a1' }, decisionId: 'd1', actualActionId: 'check' });
  await client.atoms.completeGuided('run-1');
  await client.atoms.getEvents('run-1');
  assert.deepEqual(seen.map(({ method, path, body }) => ({ method, path, body })), [
    { method: 'POST', path: '/api/v1/memory/ingest', body: { content: 'updated', request_key: 'source-2', source_id: 's1', expected_revision: 2, source_kind: 'message', durable: false } },
    { method: 'GET', path: '/api/v1/memory/items?limit=500', body: undefined },
    { method: 'POST', path: '/api/v1/runs/run-1/steps/next', body: undefined },
    { method: 'POST', path: '/api/v1/runs/run-1/steps/attempt-1/submit', body: { attempt_token: 'opaque-token', outputs: { report: 'a1' }, decision_id: 'd1', actual_action_id: 'check' } },
    { method: 'POST', path: '/api/v1/runs/run-1/complete', body: undefined },
    { method: 'GET', path: '/api/v1/runs/run-1/events', body: undefined },
  ]);
  assert.ok(seen.every(({ headers }) => headers.get('authorization') === 'Bearer gateway-key' && headers.get('x-auth-type') === 'api_key' && !headers.has('x-user-id')));
  await client.close();
});

test('v3 Memory bounds reject invalid values before any request', async () => {
  let requests = 0;
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async () => { requests++; return new Response('{}'); } });
  for (const limit of [0, 501, 1.5, Number.NaN]) await assert.rejects(client.atoms.getMemoryItems({ limit }), /limit/i);
  for (const tokenBudget of [0, 8001, 1.5, Number.NaN]) await assert.rejects(client.atoms.memoryRecall({ query: 'q', tokenBudget }), /tokenBudget/i);
  assert.equal(requests, 0);
  await client.close();
});

test('guided prepare maps executionMode and rejects unavailable managed mode', async () => {
  const seen: Array<{ path: string; body: unknown }> = [];
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    seen.push({ path: new URL(String(input)).pathname, body: JSON.parse(String(init?.body)) });
    return new Response('{}');
  } });
  await assert.rejects(async () => client.atoms.prepare({ objective: 'work', key: 'k1', executionMode: 'managed' as any }), /executionMode/i);
  await client.atoms.prepare({ objective: 'work', key: 'k2', executionMode: 'guided' });
  assert.equal(seen.length, 1);
  assert.equal(seen[0]?.path, '/api/v1/runs/prepare');
  assert.deepEqual(seen[0]?.body, { objective: 'work', task_family: 'general', input_artifact_ids: [], execution_mode: 'guided' });
  await client.close();
});
