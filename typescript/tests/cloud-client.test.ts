import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createServer, type IncomingHttpHeaders } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { RSIClient, RSIError } from '../src/rsi/index.js';

type Seen = {
  method: string;
  url: string;
  headers: IncomingHttpHeaders;
  body: any;
};

async function withService(
  replies: Record<string, [number, unknown]>,
  run: (base_url: string, seen: Seen[]) => Promise<void>,
) {
  const seen: Seen[] = [];
  const server = createServer((request, response) => {
    const chunks: Buffer[] = [];
    request.on('data', (chunk: Buffer) => chunks.push(chunk));
    request.on('end', async () => {
      const content = Buffer.concat(chunks);
      const contentType = request.headers['content-type'] ?? '';
      const body = contentType.startsWith('multipart/form-data')
        ? await new Response(new Uint8Array(content), { headers: { 'content-type': contentType } }).formData()
        : content.length ? JSON.parse(content.toString('utf8')) : undefined;
      seen.push({
        method: request.method!,
        url: request.url!,
        headers: request.headers,
        body,
      });
      const path = request.url!.split('?')[0];
      const [status, payload] = replies[`${request.method} ${path}`] ?? [
        200,
        { ok: true },
      ];
      response.statusCode = status;
      if (payload instanceof Uint8Array) {
        response.setHeader('content-type', 'application/zip');
        response.end(payload);
        return;
      }
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify(payload));
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}/api`, seen);
  } finally {
    server.close();
  }
}

test('new RSIClient() is cloud; data_dir selects local', () => {
  const previous = process.env.COSENS_API_KEY;
  process.env.COSENS_API_KEY = 'k';
  try {
    assert.equal(new RSIClient().mode, 'cloud');
    const dir = mkdtempSync(join(tmpdir(), 'rsi-mode-'));
    const local = new RSIClient({ data_dir: dir, config: null });
    assert.equal(local.mode, 'local');
    local.close();
    delete process.env.COSENS_API_KEY;
    assert.throws(() => new RSIClient(), /COSENS_API_KEY/);
  } finally {
    if (previous === undefined) delete process.env.COSENS_API_KEY;
    else process.env.COSENS_API_KEY = previous;
  }
});

test('cloud create uploads only the named files', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'rsi-cloud-'));
  writeFileSync(join(dir, 'context.txt'), '[user] weekly report');
  writeFileSync(join(dir, 'sales.csv'), 'region,amount\n');
  writeFileSync(join(dir, 'chart.png'), Buffer.from([0x89, 0x50, 0x00, 0xff]));
  writeFileSync(join(dir, 'RECIPE.md'), 'Group sales by region.');
  writeFileSync(join(dir, 'unrelated.txt'), 'stays local');
  await withService({}, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    await client.execution_create({
      evidence: {
        context_path: join(dir, 'context.txt'),
        files: [
          { path: join(dir, 'sales.csv'), role: 'input' },
          { path: join(dir, 'chart.png'), role: 'artifact' },
        ],
      },
      idempotency_key: 'delivery-1',
      recipe_reference: { path: join(dir, 'RECIPE.md'), recipe_id: 'recipe_1' },
    });
    const [request] = seen;
    assert.equal(request.method, 'POST');
    assert.equal(request.url, '/api/executions');
    assert.equal(request.headers.authorization, 'Bearer k');
    assert.match(String(request.headers['user-agent']), /^cosens-sdk-typescript\//);
    assert.deepEqual(request.body.context, {
      name: 'context.txt',
      content: '[user] weekly report',
      encoding: 'utf-8',
    });
    assert.deepEqual(
      request.body.files.map((file: { name: string }) => file.name),
      ['sales.csv', 'chart.png', 'RECIPE.md'],
    );
    assert.equal(request.body.files[1].encoding, 'base64');
    assert.deepEqual(request.body.files[2], {
      name: 'RECIPE.md',
      content: 'Group sales by region.',
      encoding: 'utf-8',
      role: 'recipe',
      recipe_id: 'recipe_1',
    });
    assert.equal('recipe_reference' in request.body, false);
    assert.equal(JSON.stringify(request.body).includes('stays local'), false);
  });
});

test('cloud reads map to routes and errors keep their codes', async () => {
  await withService(
    {
      'GET /api/executions/missing': [
        404,
        { error: { code: 'not_found', message: 'not found' } },
      ],
    },
    async (base_url, seen) => {
      const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
      await client.execution_list({ execution_ids: ['a', 'b'] });
      await client.recipe_get('r1', 'active');
      await client.recipe_version_set_archived('r1', {
        version_id: 'v1',
        archived: true,
      });
      assert.deepEqual(
        seen.map((s) => `${s.method} ${s.url}`),
        [
          'GET /api/executions?execution_ids=a%2Cb',
          'GET /api/recipes/r1?version=active',
          'PUT /api/recipes/r1/versions/v1/archived',
        ],
      );
      assert.deepEqual(await client.recover(), { started: 0 });
      assert.deepEqual(await client.recover({ recipe_id: 'recipe_1' }), { started: 0 });
      await assert.rejects(
        client.recover({ recipe_id: 'recipe_1', retry_key: 'click-1' }),
        { code: 'invalid_state' },
      );
      await assert.rejects(
        client.execution_get('missing'),
        (error: unknown) =>
          error instanceof RSIError &&
          error.code === 'not_found' &&
          error.status === 404,
      );
    },
  );
});

test('virtual user selection is isolated per cloud client', async (t) => {
  const seen: Array<string | null> = [];
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    seen.push(new Headers(init?.headers).get('x-virtual-user-id'));
    return Response.json({});
  });
  const a = new RSIClient({ mode: 'cloud', api_key: 'k', virtualUserId: 'vu_a' });
  const b = new RSIClient({ mode: 'cloud', api_key: 'k', virtualUserId: 'vu_b' });
  const ordinary = new RSIClient({ mode: 'cloud', api_key: 'k' });
  await Promise.all([a.execution_get('a'), b.execution_get('b'), ordinary.execution_get('c')]);
  assert.deepEqual(seen, ['vu_a', 'vu_b', null]);
  assert.throws(() => new RSIClient({ data_dir: '/tmp/nope', config: null, virtualUserId: 'vu_a' } as never));
});

test('virtual_user_id is rejected before any request; only virtualUserId sets the header', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({}));
  const snake = { name: 'TypeError', message: /virtual_user_id is not an option; use virtualUserId/ };
  for (const options of [
    { virtual_user_id: 'vu_a' },
    { virtual_user_id: '' },
    { virtual_user_id: undefined },
    { virtualUserId: 'vu_b', virtual_user_id: 'vu_b' },
    { virtualUserId: 'vu_b', virtual_user_id: 'vu_c' },
  ])
    assert.throws(() => new RSIClient({ mode: 'cloud', api_key: 'k', ...options } as never), snake);
  for (const bad of ['', 'vu a', 'vu\n', 7])
    assert.throws(
      () => new RSIClient({ mode: 'cloud', api_key: 'k', virtualUserId: bad as never }),
      { name: 'TypeError', message: /virtualUserId must be a nonempty header value/ },
    );
  assert.equal(fetch.mock.callCount(), 0);
  assert.throws(() => new RSIClient({ data_dir: '/tmp/nope', config: null, virtual_user_id: 'vu_a' } as never));
});

test('cloud recipe_insert uploads the validated Markdown', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'rsi-cloud-recipe-'));
  const markdown =
    '---\nname: Weekly report\ndescription: Sales by region\n---\nGroup sales by region.\n';
  writeFileSync(join(dir, 'RECIPE.md'), markdown);
  writeFileSync(join(dir, 'bad.md'), 'Group sales by region.');
  const reply = { id: 'recipe_1', active_version_id: 'version_1' };
  await withService({ 'POST /api/recipes': [200, reply] }, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    assert.deepEqual(await client.recipe_insert(join(dir, 'RECIPE.md')), reply);
    assert.deepEqual(
      seen.map(({ method, url, body }) => [method, url, body]),
      [['POST', '/api/recipes', { markdown }]],
    );
    await assert.rejects(
      client.recipe_insert(join(dir, 'bad.md')),
      (error: unknown) =>
        error instanceof RSIError && error.code === 'invalid_argument',
    );
    assert.equal(seen.length, 1);
  });
});

test('cloud timeout is in seconds and must be positive', async () => {
  assert.throws(
    () => new RSIClient({ mode: 'cloud', api_key: 'k', timeout: 0 }),
    /timeout must be a positive number of seconds/,
  );
  const server = createServer(() => {});
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address() as AddressInfo;
  try {
    const client = new RSIClient({
      mode: 'cloud',
      api_key: 'k',
      base_url: `http://127.0.0.1:${port}/api`,
      timeout: 0.2,
    });
    await assert.rejects(client.execution_get('slow'), {
      code: 'service_unavailable',
    });
  } finally {
    server.closeAllConnections();
    server.close();
  }
});

test('cloud recipes_upsert uploads ZIP bytes from a local path and file URI', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'rsi-cloud-zip-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, 'weekly report.zip');
  const zip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0xff, 0xfe]);
  writeFileSync(path, zip);
  const reply = { recipe_id: 'recipe_1', recipe_version_id: 'version_1' };
  await withService({ 'POST /api/recipes_upsert': [200, reply] }, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    assert.deepEqual(await client.recipes_upsert(path), reply);
    assert.deepEqual(await client.recipes_upsert(pathToFileURL(path).href, {
      recipe_id: 'recipe_1', idempotency_key: 'update-1',
    }), reply);

    assert.equal(seen.length, 2);
    for (const request of seen) {
      assert.equal(request.method, 'POST');
      assert.equal(request.url, '/api/recipes_upsert');
      assert.equal(request.headers.authorization, 'Bearer k');
      assert.match(String(request.headers['content-type']), /^multipart\/form-data; boundary=/);
      const file = request.body.get('file') as File;
      assert.equal(file.name, 'weekly report.zip');
      assert.equal(file.type, 'application/zip');
      assert.deepEqual(new Uint8Array(await file.arrayBuffer()), zip);
    }
    assert.equal(seen[0].body.get('recipe_id'), null);
    assert.equal(seen[0].headers['idempotency-key'], undefined);
    assert.equal(seen[1].body.get('recipe_id'), 'recipe_1');
    assert.equal(seen[1].headers['idempotency-key'], 'update-1');
  });
});

test('cloud recipe_download returns ZIP bytes for the requested version', async () => {
  const zip = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0xff, 0xfe]);
  await withService({
    'GET /api/recipes/recipe_1/versions/version_1/download': [200, zip],
    'GET /api/recipes/recipe_1/versions/version_other/download': [
      404, { error: { code: 'not_found', message: 'missing version' } },
    ],
  }, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    assert.deepEqual(await client.recipe_download('recipe_1', 'version_1'), zip);
    await assert.rejects(
      client.recipe_download('recipe_1', 'version_other'),
      (error: unknown) => error instanceof RSIError && error.code === 'not_found',
    );
    assert.equal(seen[0].url, '/api/recipes/recipe_1/versions/version_1/download');
  });
});
