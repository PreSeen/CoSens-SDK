import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  MemoryError,
  memoryRecall,
  memoryIngest,
  getMemoryItems,
  getMemoryJob,
  getMemoryExperience,
  getMemory,
} from '../src/index.js';

function useFixtureKey(t: import('node:test').TestContext) {
  const previous = process.env.COSENS_API_KEY;
  process.env.COSENS_API_KEY = 'fixture-key';
  t.after(() => {
    if (previous === undefined) delete process.env.COSENS_API_KEY;
    else process.env.COSENS_API_KEY = previous;
  });
}

test('Memory atoms send the six 10-26 requests', async (t) => {
  const requests: Array<[string, string, unknown]> = [];
  useFixtureKey(t);
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const headers = new Headers(init?.headers);
    assert.equal(headers.get('Authorization'), 'Bearer fixture-key');
    assert.equal(headers.get('X-Auth-Type'), 'api_key');
    assert.equal(init?.redirect, 'manual');
    const parsed = new URL(String(url));
    assert.equal(parsed.origin, 'https://rsi.cosens.ai');
    requests.push([
      init!.method!,
      parsed.pathname + parsed.search,
      init?.body ? JSON.parse(String(init.body)) : null,
    ]);
    return Response.json({ items: [{ id: 'example' }] });
  });

  assert.deepEqual((await memoryRecall({ query: 'intent' })).items, [
    { id: 'example' },
  ]);
  await memoryIngest({
    content: 'decision',
    requestKey: 'request-1',
    durable: false,
    sourceId: 'source-1',
    expectedRevision: 0,
  });
  await getMemoryItems();
  await getMemoryJob('job/1');
  await getMemoryExperience('exp/1', { revision: 2 });
  await getMemory();

  const prefix = '/api/memory';
  assert.deepEqual(requests, [
    ['POST', prefix + '/recall', { query: 'intent', token_budget: 1000 }],
    [
      'POST',
      prefix + '/ingest',
      {
        content: 'decision',
        request_key: 'request-1',
        source_kind: 'decision',
        durable: false,
        source_id: 'source-1',
        expected_revision: 0,
      },
    ],
    ['GET', prefix + '/items?limit=200', null],
    ['GET', prefix + '/jobs/job%2F1', null],
    ['GET', prefix + '/experiences/exp%2F1?revision=2', null],
    ['GET', prefix, null],
  ]);
  assert.throws(() => getMemoryJob('..'), TypeError);
  assert.throws(
    () => memoryRecall({ query: 'intent', tokenBudget: 8001 }),
    TypeError,
  );
  assert.equal(requests.length, 6);
});

test('Memory atoms redact authentication and refuse gateway redirects', async (t) => {
  useFixtureKey(t);
  t.mock.method(globalThis, 'fetch', async (_, init) => {
    assert.equal(init?.redirect, 'manual');
    return Response.json(
      { error: { message: 'invalid fixture-key', code: 'denied' } },
      { status: 401, headers: { 'x-request-id': 'request-1' } },
    );
  });
  await assert.rejects(
    getMemory(),
    (error: unknown) =>
      error instanceof MemoryError &&
      error.status === 401 &&
      error.code === 'denied' &&
      error.requestId === 'request-1' &&
      error.message === 'invalid [REDACTED]',
  );
  t.mock.restoreAll();
  t.mock.method(globalThis, 'fetch', async () =>
    Response.redirect('https://example.test/', 302),
  );
  await assert.rejects(getMemory(), {
    name: 'MemoryError',
    status: 302,
  });
});

test('virtual user selection is per call and unsupported operations reject', async (t) => {
  useFixtureKey(t);
  const seen: Array<[string, string | null]> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    seen.push([new URL(String(url)).pathname, new Headers(init?.headers).get('x-virtual-user-id')]);
    return Response.json({});
  });
  await Promise.all([
    memoryRecall({ query: 'a', virtualUserId: 'vu_a' }),
    memoryIngest({ content: 'b', requestKey: 'k', virtualUserId: 'vu_b' }),
    getMemoryItems({ virtualUserId: 'vu_b' }), getMemoryJob('j', { virtualUserId: 'vu_a' }), getMemoryItems(),
  ]);
  assert.deepEqual(seen, [
    ['/api/memory/recall', 'vu_a'], ['/api/memory/ingest', 'vu_b'],
    ['/api/memory/items', 'vu_b'], ['/api/memory/jobs/j', 'vu_a'], ['/api/memory/items', null],
  ]);
  assert.throws(() => getMemory({ virtualUserId: 'vu_a' }), TypeError);
  assert.throws(() => getMemoryExperience('e', { virtualUserId: 'vu_a' }), TypeError);
});

test('virtual_user_id is rejected by every Memory call before any request', async (t) => {
  useFixtureKey(t);
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({}));
  const snake = { name: 'TypeError', message: /virtual_user_id is not an option; use virtualUserId/ };
  for (const selection of [
    { virtual_user_id: 'vu_a' },
    { virtual_user_id: '' },
    { virtual_user_id: undefined },
    { virtualUserId: 'vu_b', virtual_user_id: 'vu_b' },
  ]) {
    const options = selection as never;
    await assert.rejects(memoryRecall({ query: 'a', ...selection } as never), snake);
    await assert.rejects(memoryIngest({ content: 'b', requestKey: 'k', ...selection } as never), snake);
    await assert.rejects(getMemoryItems(options), snake);
    await assert.rejects(getMemoryJob('j', options), snake);
    assert.throws(() => getMemory(options), snake);
    assert.throws(() => getMemoryExperience('e', options), snake);
  }
  await assert.rejects(
    getMemoryItems({ virtualUserId: '' }),
    { name: 'TypeError', message: /virtualUserId must be a nonempty header value/ },
  );
  assert.equal(fetch.mock.callCount(), 0);
});

test('explicit Memory service and key are per call and fail closed', async (t) => {
  useFixtureKey(t);
  const seen: Array<[string, string | null, string | null, string | null]> = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const h = new Headers(init?.headers);
    seen.push([String(url), h.get('authorization'), h.get('x-cosens-auth-type'), h.get('x-virtual-user-id')]);
    assert.equal(init?.redirect, 'manual');
    return Response.json({});
  });
  await Promise.all([
    memoryRecall({ query: 'a', baseUrl: 'https://regional.example/memory', apiKey: 'region-key', virtualUserId: 'vu_a' }),
    getMemoryItems(),
    memoryIngest({content: 'b', requestKey: 'k', baseUrl: 'https://stage.example/api/memory/', apiKey: 'stage-key'}),
    getMemoryJob('j', {baseUrl: 'https://regional.example/memory', apiKey: 'region-key'}),
    getMemoryExperience('e', {revision: 2, baseUrl: 'https://regional.example/memory', apiKey: 'region-key'}),
    getMemory({baseUrl: 'https://stage.example/api/memory/', apiKey: 'stage-key'}),
  ]);
  assert.deepEqual(seen, [
    ['https://regional.example/memory/recall', 'Bearer region-key', 'api_key', 'vu_a'],
    ['https://rsi.cosens.ai/api/memory/items?limit=200', 'Bearer fixture-key', null, null],
    ['https://stage.example/api/memory/ingest', 'Bearer stage-key', 'api_key', null],
    ['https://regional.example/memory/jobs/j', 'Bearer region-key', 'api_key', null],
    ['https://regional.example/memory/experiences/e?revision=2', 'Bearer region-key', 'api_key', null],
    ['https://stage.example/api/memory', 'Bearer stage-key', 'api_key', null],
  ]);
  await assert.rejects(getMemory({baseUrl: 'https://regional.example/memory'}), TypeError);
  await assert.rejects(getMemoryExperience('e', {apiKey: 'explicit'}), TypeError);
  await assert.rejects(getMemory({baseUrl: 'http://example.test/memory', apiKey: 'explicit'}), TypeError);
  assert.throws(() => getMemory({baseUrl: 'https://regional.example/memory', apiKey: 'explicit', virtualUserId: 'vu_a'}), TypeError);
  for (const baseUrl of ['http://example.test/a', 'https://u:p@example.test/a', 'https://example.test/a?x=1', 'https://example.test/a#x', 'https://example.test/a/../b', 'https://example.test/a/%2e%2e/b', 'https://example.test/a%2fb', 'https://example.test/a\\b', 'https://@example.test/a', 'https://example.test/a/%00b'])
    await assert.rejects(memoryRecall({query:'a', baseUrl, apiKey:'explicit'}), TypeError);
  await assert.rejects(memoryRecall({query:'a', baseUrl:'https://regional.example/memory'}), TypeError);
  await assert.rejects(memoryRecall({query:'a', apiKey:'explicit'}), TypeError);
  assert.equal(seen.length, 6);
});

test('a null explicit key never sends the ambient key to a custom service', async (t) => {
  useFixtureKey(t);
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async () => {
    calls++;
    return Response.json({});
  });
  await assert.rejects(
    memoryRecall({
      query: 'a',
      baseUrl: 'https://regional.example/memory',
      apiKey: null as unknown as string,
    }),
    TypeError,
  );
  assert.equal(calls, 0);
});
