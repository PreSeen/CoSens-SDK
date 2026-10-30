import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient } from '../src/index.ts';
import { Transport } from '../src/http.ts';

test('atomic client retains the gateway prefix, Bearer identity and wire fields', async () => {
  const seen: Array<{ url: URL; init: RequestInit }> = [];
  const fetcher: typeof fetch = async (input, init) => {
    seen.push({ url: new URL(String(input)), init: init ?? {} });
    if (seen.length === 1) return new Response(JSON.stringify({ status: 'not_found' }), { status: 200 });
    return new Response(JSON.stringify({ items: [] }), { status: 200 });
  };
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: fetcher, region: 'cn' });
  const recalled = await client.atoms.recallContext({ query: 'hello', taskFamily: 'general' });
  assert.equal(seen[0]!.url.pathname, '/api/v1/recipes/active');
  assert.equal(seen[0]!.url.searchParams.get('task_family'), 'general');
  assert.equal(seen[1]!.url.pathname, '/api/v1/memory/recall');
  assert.deepEqual(JSON.parse(String(seen[1]!.init.body)), { query: 'hello', token_budget: 1000 });
  assert.equal(new Headers(seen[0]!.init.headers).get('Authorization'), 'Bearer friday-key');
  assert.ok(seen.every(({ init }) => new Headers(init.headers).get('Authorization') === 'Bearer friday-key'));
  assert.ok(seen.every(({ init }) => new Headers(init.headers).get('X-Auth-Type') === 'api_key'));
  assert.deepEqual(recalled.snapshot.evidence, []);
  assert.equal(recalled.run_id, undefined);
  await client.close();
});

test('atomic client uses one stable prepare key and decodes server errors', async () => {
  const seen: Array<{ url: URL; init: RequestInit }> = [];
  const fetcher: typeof fetch = async (input, init) => {
    seen.push({ url: new URL(String(input)), init: init ?? {} });
    if (seen.length === 1) return new Response(JSON.stringify({ status: 'processing', retry_after_seconds: 0 }), { status: 200 });
    if (seen.length === 2) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }), { status: 200 });
    return new Response(JSON.stringify({ error: { code: 'bad', message: 'bad request' } }), { status: 400, headers: { 'x-request-id': 'req-1' } });
  };
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: fetcher });
  const ready = await client.atoms.prepareReady({ objective: 'task', key: 'turn-1' });
  assert.equal(ready.run_id, 'run-1');
  assert.equal(new Headers(seen[0]!.init.headers).get('Idempotency-Key'), 'turn-1');
  assert.equal(new Headers(seen[1]!.init.headers).get('Idempotency-Key'), 'turn-1');
  await assert.rejects(client.atoms.getRun('run-1'), (error: any) => error.statusCode === 400 && error.code === 'bad' && error.requestId === 'req-1');
  assert.ok(seen.every(({ init }) => new Headers(init.headers).get('X-Auth-Type') === 'api_key'));
  await client.close();
});

test('atomic polling rejects non-finite timeouts before contacting the gateway', async () => {
  let requests = 0;
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: async () => {
    requests++;
    return new Response(JSON.stringify({ run_id: 'r1', status: 'ready' }));
  } });
  for (const invalid of [Number.NaN, Number.POSITIVE_INFINITY, -1]) {
    await assert.rejects(client.atoms.prepareReady({ objective: 'task', key: 'turn-1' }, invalid), /timeout/i);
    await assert.rejects(client.atoms.waitImprovement('job-1', { timeout: invalid }), /timeout/i);
  }
  assert.equal(requests, 0);
  await client.close();
});

test('administrator requests use the same CoSens authentication headers', async () => {
  const seen: RequestInit[] = [];
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: async (_input, init) => {
    seen.push(init ?? {});
    return new Response(JSON.stringify({ status: 'published' }), { status: 200 });
  } });
  await client.admin.atoms.publish('job-1', { expectedRevision: 2, reason: 'verified' });
  assert.equal(new Headers(seen[0]!.headers).get('Authorization'), 'Bearer friday-key');
  assert.equal(new Headers(seen[0]!.headers).get('X-Auth-Type'), 'api_key');
  await client.close();
});

test('dot-segment resource IDs cannot escape their API route', async () => {
  let requests = 0;
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: async () => { requests++; return new Response('{}'); } });
  await assert.rejects(async () => client.atoms.getRun('..'), /resource ID|path segment/i);
  await assert.rejects(async () => client.atoms.getRun('.'), /resource ID|path segment/i);
  assert.equal(requests, 0);
  await client.close();
});

test('transport rejects duplicate API prefixes and backslash paths before authentication', async () => {
  let requests = 0;
  const transport = new Transport({ apiKey: 'key', fetch: async () => { requests++; return new Response('{}'); } });
  for (const path of ['/api/v1/runs/r1', '/api?debug=1', '/v1\\runs\\r1']) {
    await assert.rejects(transport.request('GET', path), /service-relative path|without \/api/i);
  }
  assert.equal(requests, 0);
});

test('gateway error text cannot echo the Friday Key', async () => {
  const client = new CoSensClient({ apiKey: 'secret-key', fetch: async () => new Response(JSON.stringify({ error: { code: 'denied', message: 'Rejected Bearer secret-key' } }), { status: 401 }) });
  await assert.rejects(client.atoms.getRun('r1'), (error: any) => {
    assert.equal(error.statusCode, 401);
    assert.equal(error.code, 'denied');
    assert.equal(error.message.includes('secret-key'), false);
    return true;
  });
  await client.close();
});

test('every public and administrator atom preserves its documented wire route and body', async () => {
  const seen: Array<{ method: string; path: string; body: unknown; key: string | null }> = [];
  const client = new CoSensClient({ apiKey: 'friday-key', fetch: async (input, init) => {
    seen.push({ method: init?.method ?? '', path: new URL(String(input)).pathname + new URL(String(input)).search,
      body: init?.body ? JSON.parse(String(init.body)) : undefined, key: new Headers(init?.headers).get('Idempotency-Key') });
    return new Response('{}');
  } });
  const cases: Array<[() => Promise<unknown>, string, string, unknown, string?]> = [
    [() => client.atoms.getActiveRecipe({ taskFamily: 'a/b' }), 'GET', '/api/v1/recipes/active?task_family=a%2Fb', undefined],
    [() => client.atoms.memoryRecall({ query: 'q', tokenBudget: 7 }), 'POST', '/api/v1/memory/recall', { query: 'q', token_budget: 7 }],
    [() => client.atoms.memoryIngest({ content: 'c', requestKey: 'request-1' }), 'POST', '/api/v1/memory/ingest', { content: 'c', request_key: 'request-1', source_kind: 'decision', durable: true }],
    [() => client.atoms.getMemoryJob('j/1'), 'GET', '/api/v1/memory/jobs/j%2F1', undefined],
    [() => client.atoms.getMemoryExperience('e/1', { revision: 2 }), 'GET', '/api/v1/memory/experiences/e%2F1?revision=2', undefined],
    [() => client.atoms.getMemory(), 'GET', '/api/v1/memory', undefined],
    [() => client.atoms.artifact({ name: 'a.txt', content: 'hello' }), 'POST', '/api/v1/artifacts', { name: 'a.txt', content: 'hello' }],
    [() => client.atoms.prepare({ objective: 'task', key: 'turn-1', taskFamily: 'general', inputArtifactIds: ['a1'] }), 'POST', '/api/v1/runs/prepare', { objective: 'task', task_family: 'general', input_artifact_ids: ['a1'] }, 'turn-1'],
    [() => client.atoms.getRun('r/1'), 'GET', '/api/v1/runs/r%2F1', undefined],
    [() => client.atoms.getRecipeStep('r1', 'n/1'), 'GET', '/api/v1/runs/r1/recipe/steps/n%2F1', undefined],
    [() => client.atoms.sessionEvents('r1', [{ id: 'e1' }]), 'POST', '/api/v1/runs/r1/session-events', { events: [{ id: 'e1' }] }],
    [() => client.atoms.initialInputs('r1', ['a1']), 'POST', '/api/v1/runs/r1/initial-inputs', { artifact_ids: ['a1'] }],
    [() => client.atoms.guidance('r1', { data: { x: 1 } }), 'POST', '/api/v1/runs/r1/guidance', { data: { x: 1 } }],
    [() => client.atoms.checkpoint('r1', { data: { x: 2 } }), 'POST', '/api/v1/runs/r1/checkpoint', { data: { x: 2 } }],
    [() => client.atoms.getDecision('r1', 'd/1'), 'GET', '/api/v1/runs/r1/decisions/d%2F1', undefined],
    [() => client.atoms.getTrace('r1'), 'GET', '/api/v1/runs/r1/trace', undefined],
    [() => client.atoms.submit('r1', { data: { outcome: 'completed' }, key: 'submit-1' }), 'POST', '/api/v1/runs/r1/submit', { data: { outcome: 'completed' } }, 'submit-1'],
    [() => client.atoms.feedback('r1', { data: { useful: true }, key: 'feedback-1' }), 'POST', '/api/v1/runs/r1/feedback', { data: { useful: true } }, 'feedback-1'],
    [() => client.atoms.improve('r1', { reason: 'verified', key: 'improve-1' }), 'POST', '/api/v1/runs/r1/improvements', { data: { reason: 'verified' } }, 'improve-1'],
    [() => client.atoms.getImprovement('j1'), 'GET', '/api/v1/improvements/j1', undefined],
    [() => client.atoms.resumeImprovement('j1'), 'POST', '/api/v1/improvements/j1/resume', undefined],
    [() => client.admin.atoms.bootstrapBundle({ taskFamily: 'general', bundle: { nodes: [] }, reason: 'seed' }), 'POST', '/api/internal/admin/v1/recipes/bootstrap-bundle', { task_family: 'general', bundle: { nodes: [] }, reason: 'seed' }],
    [() => client.admin.atoms.evaluateDecision('r1', 'd1', { evaluation: { score: 1 } }), 'POST', '/api/internal/admin/v1/runs/r1/decisions/d1/evaluations', { score: 1 }],
    [() => client.admin.atoms.evaluateRecipe('r1', { record: { score: 2 }, artifactId: 'a1', contentHash: 'h1' }), 'POST', '/api/internal/admin/v1/runs/r1/recipe-evaluation', { record: { score: 2 }, artifact_id: 'a1', content_hash: 'h1' }],
    [() => client.admin.atoms.publish('j1', { expectedRevision: 3, reason: 'verified' }), 'POST', '/api/internal/admin/v1/improvements/j1/publish', { expected_revision: 3, reason: 'verified' }],
  ];
  for (const [call] of cases) await call();
  assert.equal(seen.length, cases.length);
  cases.forEach(([, method, path, body, key], index) => {
    assert.deepEqual(seen[index], { method, path, body, key: key ?? null }, `atom ${index}: ${path}`);
  });
  await client.close();
});
