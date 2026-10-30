import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient } from '../src/index.ts';

test('start prepare returns frozen host request and retries with stable keys', async () => {
  const seen: Array<{ path: string; key: string | null; body: unknown }> = [];
  const client = new CoSensClient({ apiKey: 'key', fetch: async (input, init) => {
    const path = new URL(String(input)).pathname;
    seen.push({ path, key: new Headers(init?.headers).get('Idempotency-Key'), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify(path === '/api/v1/tasks' ? { task_id: 't1', status: 'ready' } :
      { execution_id: 'e1', request_sha256: 'frozen-hash', request: { kind: 'original' }, status: 'awaiting_execution' }));
  } });
  const snapshot = { task: { source: 'new_task' as const, objective: 'work', artifacts: [] } };
  const first = await client.start(snapshot, { requestKey: 'business-turn-1' });
  const second = await client.start(snapshot, { requestKey: 'business-turn-1' });
  assert.deepEqual(first.execution_request, { kind: 'original' });
  assert.equal(second.execution_id, 'e1');
  assert.deepEqual(seen.map(x => x.path), ['/api/v1/tasks', '/api/v1/tasks/t1/executions', '/api/v1/tasks', '/api/v1/tasks/t1/executions']);
  assert.equal(seen[0]!.key, seen[2]!.key);
  assert.equal(seen[1]!.key, seen[3]!.key);
  assert.notEqual(seen[0]!.key, seen[1]!.key);
  assert.deepEqual(seen[0]!.body, { source: 'new_task', objective: 'work', artifacts: [] });
});

test('disable and missing host approval make no workflow requests', async () => {
  let requests = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { requests++; return new Response('{}'); } });
  assert.deepEqual(await client.start({}, { mode: 'disable' }), { status: 'disabled', phase: 'prepare' });
  await assert.rejects(client.start({ task: { source: 'external_artifact', artifacts: ['a1'] } }, { mode: 'approval', requestKey: 'k1' }), /authorization callback/);
  await assert.rejects(client.start({ task: { source: 'external_artifact', artifacts: ['a1'] } }, { mode: 'approval', requestKey: 'k1', authorize: () => false }), /not granted/);
  assert.equal(requests, 0);
});

test('prepare rejects a ready receipt without a task ID before starting native work', async () => {
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { calls++; return new Response(JSON.stringify({ status: 'ready' })); } });
  await assert.rejects(client.start({ task: { source: 'new_task', objective: 'work' } }, { requestKey: 'k1' }), /task_id/);
  assert.equal(calls, 1);
});
