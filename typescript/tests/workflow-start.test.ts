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

test('high-level prepare forwards model, Recipe and Memory options without changing stable keys', async () => {
  const seen: Array<{ path: string; key: string | null; body: any }> = [];
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    const path = new URL(String(input)).pathname;
    seen.push({ path, key: new Headers(init?.headers).get('Idempotency-Key'), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify(path.endsWith('/executions')
      ? { execution_id: 'e1', request_sha256: 'hash', request: { method: { recipe_id: 'recipe-1' } }, status: 'awaiting_execution' }
      : { task_id: 't1', status: 'ready' }));
  } });
  const modelApi = { baseUrl: 'https://business.example/v1', model: 'business-model', apiKey: 'provider-key' };
  const options = { requestKey: 'prepare-1', modelApi, recipeId: 'recipe-1', includeMemory: true };
  const first = await client.start('Compute the business report', options);
  const repeated = await client.start('Compute the business report', options);
  const existing = await client.start({ task_id: 't1' }, options);
  assert.equal(first.execution_request.method.recipe_id, 'recipe-1');
  assert.equal(repeated.execution_id, existing.execution_id);
  assert.deepEqual(seen[0]!.body, { source: 'new_task', objective: 'Compute the business report', artifacts: [] });
  const executions = seen.filter(x => x.path.endsWith('/executions'));
  assert.equal(executions.length, 3);
  assert.ok(executions.every(x => x.key === executions[0]!.key));
  for (const execution of executions) assert.deepEqual(execution.body, {
    model_api: { base_url: modelApi.baseUrl, model: modelApi.model, api_key: modelApi.apiKey },
    recipe_id: 'recipe-1', include_memory: true,
  });
  assert.ok(!('model_api' in seen[0]!.body));
  await client.close();
});

test('high-level improve uses the Task endpoint and preserves provider error redaction', async () => {
  let sent: { path: string; key: string | null; body: any } | undefined;
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    sent = { path: new URL(String(input)).pathname, key: new Headers(init?.headers).get('Idempotency-Key'), body: JSON.parse(String(init?.body)) };
    return new Response(JSON.stringify({ detail: 'provider-secret' }), { status: 401 });
  } });
  await assert.rejects(client.start({ task_id: 't1', revision_id: 'r1', feedback: 'Review signs' }, {
    phase: 'improve', requestKey: 'review-1', modelApi: { baseUrl: 'https://business.example/v1', model: 'model', apiKey: 'provider-secret' },
  }), (error: any) => { assert.ok(!error.message.includes('provider-secret')); return true; });
  assert.deepEqual(sent, { path: '/api/v1/tasks/t1/improvements', key: 'review-1', body: {
    revision_id: 'r1', feedback: 'Review signs', model_api: { base_url: 'https://business.example/v1', model: 'model', api_key: 'provider-secret' },
  } });
  await client.close();
});

test('new prepare options preserve disable and approval boundaries', async () => {
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { calls++; return new Response('{}'); } });
  const options = { requestKey: 'prepare-1', modelApi: { baseUrl: 'https://business.example/v1', model: 'model' }, includeMemory: true };
  assert.deepEqual(await client.start('Work', { ...options, mode: 'disable' }), { status: 'disabled', phase: 'prepare' });
  await assert.rejects(client.start('Work', { ...options, mode: 'approval', authorize: () => false }), /not granted/);
  await assert.rejects(client.start(' ', options), /nonempty/);
  await assert.rejects(client.start('Work', { ...options, includeMemory: 'true' as unknown as boolean }), /includeMemory/);
  assert.equal(calls, 0);
  await client.close();
});
