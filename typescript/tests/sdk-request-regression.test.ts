/** 15 request-boundary regressions. Only the external HTTP peer is replaced. */
import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import { CoSensClient, RSIHTTPError, RSITransportError } from '../src/index.ts';

const API_KEY = 'regression-api-key';
const GRANT = { method: 'PUT', upload_url: '/v1/document/uploads/regression_token' };

function sdk(t: TestContext, options: { status?: number; payload?: unknown; raw?: string; headers?: Record<string, string> } = {}) {
  const seen: Request[] = [];
  const client = new CoSensClient({ apiKey: API_KEY, region: 'cn', fetch: async (input, init) => {
    seen.push(new Request(String(input), init));
    return new Response(options.raw ?? JSON.stringify(options.payload ?? {}), { status: options.status ?? 200, headers: options.headers });
  } });
  t.after(() => client.close());
  return { a: client.atoms, seen };
}

test('R01 maximum length key and manual replay keep identical requests', async t => {
  const { a, seen } = sdk(t, { payload: { task_id: 'task-1' } });
  for (let i = 0; i < 2; i++) assert.equal((await a.createTask({ source: 'new_task', objective: '整理资料', key: 'k'.repeat(256) })).task_id, 'task-1');
  assert.equal(seen.length, 2);
  const bodies = await Promise.all(seen.map(r => r.text()));
  assert.equal(bodies[0], bodies[1]);
  assert.deepEqual(JSON.parse(bodies[0]!), { source: 'new_task', objective: '整理资料', artifacts: [] });
  assert.ok(seen.every(r => r.headers.get('idempotency-key') === 'k'.repeat(256)));
});

test('R02 oversized key is rejected before sending', async t => {
  const { a, seen } = sdk(t);
  await assert.rejects(async () => a.startExecution('task-1', { key: 'k'.repeat(257) }), /key/);
  assert.equal(seen.length, 0);
});

test('R03 explicit empty optional key is not silently omitted', async t => {
  const { a, seen } = sdk(t);
  for (const call of [() => a.submit('run-1', { key: '' }), () => a.feedback('run-1', { data: {}, key: '' }), () => a.improve('run-1', { reason: 'revise', key: '' })]) {
    await assert.rejects(async () => call(), /key/);
  }
  assert.equal(seen.length, 0);
});

test('R04 control characters in keys are rejected before sending', async t => {
  const { a, seen } = sdk(t);
  for (const key of ['safe\r\nX-Injected: yes', 'nul\x00key', 'del\x7fkey']) {
    await assert.rejects(async () => a.startExecution('task-1', { key }), TypeError);
  }
  assert.equal(seen.length, 0);
});

test('R05 memory request key stays in JSON and preserves false and zero', async t => {
  const { a, seen } = sdk(t);
  await a.memoryIngest({ content: 'test memory', requestKey: 'source-key', sourceKind: 'message', durable: false, expectedRevision: 0 });
  assert.equal(seen.length, 1);
  assert.equal(seen[0]!.headers.get('idempotency-key'), null);
  assert.deepEqual(await seen[0]!.json(), { content: 'test memory', request_key: 'source-key', source_kind: 'message', durable: false, expected_revision: 0 });
});

test('R06 key does not leak into following read request', async t => {
  const { a, seen } = sdk(t);
  await a.startExecution('task-1', { key: 'execution-key' });
  await a.getTask('task-1');
  await a.submit('run-1');
  assert.equal(seen.length, 3);
  assert.equal(seen[0]!.headers.get('idempotency-key'), 'execution-key');
  assert.ok(seen.slice(1).every(r => r.headers.get('idempotency-key') === null));
  assert.ok(seen.every(r => r.headers.get('authorization') === `Bearer ${API_KEY}` && r.headers.get('x-auth-type') === 'api_key'));
});

test('R07 conflict preserves diagnostics and is not retried', async t => {
  const { a, seen } = sdk(t, { status: 409, payload: { error: { code: 'idempotency_conflict', message: 'request body changed' } }, headers: { 'x-request-id': 'request-409' } });
  await assert.rejects(a.startExecution('task-1', { key: 'conflict-key' }), (error: unknown) => {
    assert.ok(error instanceof RSIHTTPError);
    assert.deepEqual([error.statusCode, error.code, error.requestId, error.message], [409, 'idempotency_conflict', 'request-409', 'request body changed']);
    return true;
  });
  assert.equal(seen.length, 1);
  const unauthenticated = sdk(t, { status: 401, payload: { detail: { code: 'unauthenticated', message: 'invalid bearer credential' } }, headers: { 'x-request-id': 'request-401' } });
  await assert.rejects(unauthenticated.a.getTask('task-1'), (error: unknown) => {
    assert.ok(error instanceof RSIHTTPError);
    assert.deepEqual([error.statusCode, error.code, error.requestId, error.message], [401, 'unauthenticated', 'request-401', 'invalid bearer credential']);
    return true;
  });
  assert.equal(unauthenticated.seen.length, 1);
  const sensitive = sdk(t, { status: 401, payload: { detail: { code: API_KEY, message: `bad ${API_KEY}` } }, headers: { 'x-request-id': API_KEY } });
  await assert.rejects(sensitive.a.getTask('task-1'), (error: unknown) => {
    assert.ok(error instanceof RSIHTTPError);
    assert.ok(!error.message.includes(API_KEY) && !error.code?.includes(API_KEY) && !error.requestId?.includes(API_KEY));
    return true;
  });
  assert.equal(sensitive.seen.length, 1);
});

test('R08 rate limit does not trigger automatic mutation retry', async t => {
  const { a, seen } = sdk(t, { status: 429, payload: { detail: 'rate limited' }, headers: { 'retry-after': '0' } });
  await assert.rejects(a.startExecution('task-1', { key: 'rate-key' }), (error: unknown) => {
    assert.ok(error instanceof RSIHTTPError); assert.equal(error.statusCode, 429); return true;
  });
  assert.equal(seen.length, 1);
});

test('R09 malformed success body reports uncertain upload without retry', async t => {
  const { a, seen } = sdk(t, { raw: '{not-json' });
  await assert.rejects(a.uploadDocumentToGrant(GRANT, Buffer.from('payload')), (error: unknown) => {
    assert.ok(error instanceof RSITransportError); assert.match(error.message, /invalid JSON/); return true;
  });
  assert.equal(seen.length, 1); assert.equal(seen[0]!.method, 'PUT');
  let calls = 0;
  const interrupted = new CoSensClient({ apiKey: API_KEY, region: 'cn', fetch: async () => {
    calls++;
    return new Response(new ReadableStream({ start(controller) { controller.error(new Error(`socket interrupted ${API_KEY}`)); } }));
  } });
  t.after(() => interrupted.close());
  await assert.rejects(interrupted.atoms.uploadDocumentToGrant(GRANT, Buffer.from('payload')), (error: unknown) => {
    assert.ok(error instanceof RSITransportError);
    assert.match(error.message, /response.*read/i);
    assert.ok(!error.message.includes(API_KEY));
    return true;
  });
  assert.equal(calls, 1);
});

test('R10 non JSON gateway error keeps status and request ID', async t => {
  const { a, seen } = sdk(t, { status: 502, raw: '<html>bad gateway</html>', headers: { 'x-request-id': 'proxy-502' } });
  await assert.rejects(a.getTask('task-1'), (error: unknown) => {
    assert.ok(error instanceof RSIHTTPError); assert.deepEqual([error.statusCode, error.requestId], [502, 'proxy-502']); return true;
  });
  assert.equal(seen.length, 1);
});

test('R11 concurrent requests keep their own keys and bodies', { timeout: 3000 }, async t => {
  const seen: Request[] = [];
  let release!: () => void;
  const entered = new Promise<void>(resolve => { release = resolve; });
  const client = new CoSensClient({ apiKey: API_KEY, region: 'cn', fetch: async (input, init) => {
    const request = new Request(String(input), init); seen.push(request);
    if (seen.length === 2) release();
    await entered;
    const body = await request.clone().json();
    return new Response(JSON.stringify({ objective: body.objective }));
  } });
  t.after(() => client.close());
  const results = await Promise.all([
    client.atoms.createTask({ source: 'new_task', objective: 'alpha', key: 'key-alpha' }),
    client.atoms.createTask({ source: 'new_task', objective: 'beta', key: 'key-beta' }),
  ]);
  assert.deepEqual(results.map(r => r.objective), ['alpha', 'beta']);
  assert.deepEqual((await Promise.all(seen.map(async r => [r.headers.get('idempotency-key'), (await r.json()).objective]))).sort(), [['key-alpha', 'alpha'], ['key-beta', 'beta']]);
});

test('R12 upload exact byte limit and timezone offset are accepted', async t => {
  const { a, seen } = sdk(t, { payload: { document_id: 'doc-1' } });
  for (const [index, expires_at] of ['2099-01-01T08:00:00+08:00', '2104-02-29T00:00:00Z', '2400-02-29T00:00:00.123456Z'].entries()) {
    const receipt = await a.uploadDocumentToGrant({ method: 'PUT', upload_url: `https://friday-rsi-cn.stardust-ai.com:443/api/v1/document/uploads/token_${index}`, expires_at, max_bytes: 6 }, Buffer.from('中文'));
    assert.equal(receipt.document_id, 'doc-1');
  }
  assert.equal(seen.length, 3);
  for (const request of seen) assert.deepEqual(new Uint8Array(await request.arrayBuffer()), new Uint8Array([228, 184, 173, 230, 150, 135]));
});

test('R13 impossible calendar expiry is rejected before consuming token', async t => {
  const { a, seen } = sdk(t);
  for (const expires_at of ['2099-02-30T00:00:00Z', '2100-02-29T00:00:00Z', '2099-04-31T00:00:00+08:00', '2099-01-01T24:00:00Z', '2099-01-01T00:00:00']) {
    await assert.rejects(a.uploadDocumentToGrant({ ...GRANT, expires_at }, Buffer.from('x')), /expiry/);
  }
  assert.equal(seen.length, 0);
});

test('R14 reserved characters in resource ID remain one encoded segment', async t => {
  const { a, seen } = sdk(t);
  await a.getTask('a/b?c#d%2F');
  assert.equal(seen.length, 1);
  assert.equal(new URL(seen[0]!.url).pathname, '/api/v1/tasks/a%2Fb%3Fc%23d%252F');
  assert.equal(new URL(seen[0]!.url).search, '');
});

test('R15 upload port zero is not treated as default HTTPS port', async t => {
  const { a, seen } = sdk(t);
  await assert.rejects(a.uploadDocumentToGrant({ ...GRANT, upload_url: 'https://friday-rsi-cn.stardust-ai.com:0/api/v1/document/uploads/regression_token' }, Buffer.from('x')), /upload target/);
  assert.equal(seen.length, 0);
});
