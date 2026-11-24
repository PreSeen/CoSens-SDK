import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import Database from 'better-sqlite3';
import { createServer, type IncomingHttpHeaders } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { RSIClient, RSIError } from '../src/rsi/index.js';
import { Store } from '../src/rsi/store.js';
import type { Recipe } from '../src/rsi/models.js';

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


test('local ZIP upload adopts cloud Recipe and version IDs and keeps Execution linkage', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-local-zip-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const path = join(data_dir, 'RECIPE.md');
  const zip = join(data_dir, 'recipe.zip');
  const firstMarkdown = '---\nname: Report\ndescription: Generate the report\n---\nDraft one\n';
  writeFileSync(path, firstMarkdown);
  writeFileSync(zip, new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1]));
  const local = new RSIClient({
    data_dir,
    config: {
      model: 'fixture', base_url: 'http://localhost:9/v1', auto_start: false,
      headers: {
        Authorization: 'Bearer fixture-access-token',
        'X-Auth-Type': 'access_token',
        'X-Virtual-User-Id': 'vu_fixture',
        'Content-Type': 'application/json',
        'Content-Length': '0',
        'Idempotency-Key': 'model-key',
      },
    },
  });
  t.after(() => local.close());
  const recipe = await local.recipe_insert(path);
  const firstLocalVersion = recipe.active_version_id!;
  writeFileSync(join(data_dir, recipe.storage_path, firstLocalVersion, 'helper.py'), 'print(42)\n');
  const contextPath = join(data_dir, 'context.md');
  writeFileSync(contextPath, 'Example task context');
  const execution = await local.execution_create({
    evidence: { context_path: contextPath, files: [] },
    idempotency_key: 'recipe-upload-execution',
    recipe_reference: { path, recipe_id: recipe.id },
  });
  const first = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud_1' };
  const second = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud_2' };
  const replies: Record<string, [number, unknown]> = { 'POST /api/recipes_upsert': [200, first] };
  await withService(replies, async (base_url, seen) => {
    const initial = { base_url, local_recipe_id: recipe.id, local_recipe_version_id: firstLocalVersion };
    await assert.rejects(local.recipes_upsert(zip, initial), /finish the pending Recipe execution/);
    assert.equal(seen.length, 0);
    const setup = new Database(join(data_dir, 'rsi-ts.sqlite3'));
    try {
      setup.prepare(`UPDATE executions SET status='completed', recipe_version_id=?,
        details=json_set(details, '$.test_results', json(?)) WHERE id=?`)
        .run(firstLocalVersion, JSON.stringify([{
          id: 'test-persisted', recipe_id: recipe.id, version_id: firstLocalVersion,
        }]), execution.id);
      setup.prepare('INSERT INTO idempotency VALUES (?,?,?,?)').run(
        'recipe_retry', 'old-retry-key', JSON.stringify({ recipe_id: recipe.id }),
        JSON.stringify({ id: execution.id, fence: 0 }),
      );
    } finally {
      setup.close();
    }
    assert.deepEqual(await local.recipes_upsert(pathToFileURL(zip), initial), first);
    assert.deepEqual(await local.recipes_upsert(zip, { ...initial, virtualUserId: 'vu_fixture' }), first);
    await assert.rejects(local.recipes_upsert(zip, { ...initial, virtualUserId: 'vu_other' }), /different arguments/);
    assert.equal(seen[0].body.get('recipe_id'), null);
    assert.equal(seen[0].headers.authorization, 'Bearer fixture-access-token');
    assert.equal(seen[0].headers['x-auth-type'], 'access_token');
    assert.equal(seen[0].headers['x-virtual-user-id'], 'vu_fixture');
    assert.match(String(seen[0].headers['content-type']), /^multipart\/form-data; boundary=/);
    assert.match(String(seen[0].headers['idempotency-key']), /^local-recipe-upload-[0-9a-f]{64}$/);
    const synced = await local.recipe_get(recipe.id, firstLocalVersion);
    assert.equal(synced.id, first.recipe_id);
    assert.equal(synced.selected_version?.id, first.recipe_version_id);
    assert.equal(synced.storage_path, recipe.storage_path);
    assert.equal(readFileSync(join(data_dir, synced.storage_path, first.recipe_version_id, 'helper.py'), 'utf8'), 'print(42)\n');
    const resumed = await local.execution_get(execution.id);
    assert.equal(resumed.id, execution.id);
    assert.equal(resumed.recipe_id, first.recipe_id);
    assert.equal(resumed.recipe_version_id, first.recipe_version_id);
    assert.deepEqual(resumed.test_results, [{
      id: 'test-persisted', recipe_id: first.recipe_id, version_id: first.recipe_version_id,
    }]);
    const listed = await local.recipe_list({ recipe_ids: [recipe.id, first.recipe_id] });
    assert.equal(listed.length, 1);
    assert.equal(listed[0].recipe_id, first.recipe_id);
    assert.deepEqual(await local.recover({ recipe_id: first.recipe_id, retry_key: 'old-retry-key' }), {
      started: 0, recipe_id: first.recipe_id, execution_id: execution.id,
    });

    const store = new Store(data_dir);
    let secondLocalVersion: string;
    try {
      secondLocalVersion = store.tx(() => {
        const version = store.append(store.get<Recipe>(recipe.id, 'recipe'), {
          name: 'Report', description: 'Generate the report', instructions: 'Draft two',
        });
        store.publishRecipeVersion(recipe.id, version.id);
        return version.id;
      });
    } finally {
      store.close();
    }
    const nextZip = join(data_dir, 'recipe-v2.zip');
    writeFileSync(nextZip, new Uint8Array([0x50, 0x4b, 0x03, 0x04, 2]));
    replies['POST /api/recipes_upsert'] = [200, second];
    assert.deepEqual(await local.recipes_upsert(nextZip, {
      ...initial, local_recipe_version_id: secondLocalVersion,
    }), second);
    assert.equal(seen[1].body.get('recipe_id'), first.recipe_id);
    assert.deepEqual(await local.recipes_upsert(zip, initial), first);
    assert.deepEqual(await local.recipes_upsert(zip, {
      ...initial, local_recipe_id: first.recipe_id, local_recipe_version_id: first.recipe_version_id,
    }), first);
    assert.equal(seen.length, 2);

    const reopened = new RSIClient({ data_dir, config: null });
    try {
      const current = await reopened.recipe_get(recipe.id);
      assert.equal(current.id, second.recipe_id);
      assert.equal(current.selected_version?.id, second.recipe_version_id);
      const previous = await reopened.recipe_get(recipe.id, firstLocalVersion);
      assert.equal(previous.selected_version?.id, first.recipe_version_id);
      assert.equal(readFileSync(join(data_dir, current.storage_path, first.recipe_version_id, 'RECIPE.md'), 'utf8'), firstMarkdown);
      assert.equal(readFileSync(join(data_dir, current.storage_path, firstLocalVersion, 'RECIPE.md'), 'utf8'), firstMarkdown);
    } finally {
      reopened.close();
    }
    const db = new Database(join(data_dir, 'rsi-ts.sqlite3'), { readonly: true });
    try {
      assert.deepEqual(db.prepare('SELECT id FROM recipes').all(), [{ id: first.recipe_id }]);
      assert.deepEqual(db.prepare('SELECT id FROM versions WHERE recipe_id=? ORDER BY number').all(first.recipe_id), [
        { id: first.recipe_version_id }, { id: second.recipe_version_id },
      ]);
      assert.deepEqual(db.prepare('SELECT id,recipe_id,recipe_version_id FROM executions').get(), {
        id: execution.id, recipe_id: first.recipe_id, recipe_version_id: first.recipe_version_id,
      });
      assert.deepEqual(db.pragma('foreign_key_check'), []);
      assert.equal(db.prepare('SELECT 1 FROM idempotency WHERE scope=?').get('recipe_cloud_upload_pending'), undefined);
      assert.ok(!JSON.stringify(db.prepare('SELECT request,result FROM idempotency').all()).includes(initial.api_key));
    } finally {
      db.close();
    }
    const alias = await local.recipe_insert(path);
    await assert.rejects(local.recipes_upsert(zip, {
      ...initial, local_recipe_id: alias.id, local_recipe_version_id: alias.active_version_id!,
    }), /already used locally/);
    assert.equal((await local.recipe_get(alias.id)).id, alias.id);
  });
});

test('failed local ZIP upload retains a retry only for the same bytes and destination', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-local-zip-retry-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const path = join(data_dir, 'RECIPE.md');
  const zip = join(data_dir, 'recipe.zip');
  const alternate = join(data_dir, 'alternate.zip');
  writeFileSync(path, '---\nname: Report\ndescription: Generate the report\n---\nMake a report.\n');
  writeFileSync(zip, 'packed input');
  writeFileSync(alternate, 'different input');
  const local = new RSIClient({ data_dir, config: null });
  t.after(() => local.close());
  const recipe = await local.recipe_insert(path);
  const replies: Record<string, [number, unknown]> = {
    'POST /api/recipes_upsert': [422, { error: { code: 'invalid_request', message: 'ZIP invalid' } }],
  };
  await withService(replies, async (base_url, seen) => {
    const options = { base_url, api_key: 'k', local_recipe_id: recipe.id, local_recipe_version_id: recipe.active_version_id! };
    await assert.rejects(local.recipes_upsert(alternate, options), /ZIP invalid/);
    replies['POST /api/recipes_upsert'] = [503, { error: { code: 'unavailable', message: 'try again' } }];
    await assert.rejects(local.recipes_upsert(zip, options), /try again/);
    assert.equal((await local.recipe_get(recipe.id)).id, recipe.id);
    await assert.rejects(local.recipes_upsert(alternate, options), /another upload is pending/);
    await assert.rejects(local.recipes_upsert(zip, { ...options, virtualUserId: 'vu_other' }), /another upload is pending/);
    await assert.rejects(local.recipes_upsert(zip, { ...options, base_url: base_url + '/other' }), /another upload is pending/);
    await assert.rejects(local.recipes_upsert(zip, { ...options, local_recipe_version_id: 'version_missing' }), /version does not belong/);
    assert.equal(seen.length, 2);
    const receipt = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud' };
    replies['POST /api/recipes_upsert'] = [200, receipt];
    assert.deepEqual(await local.recipes_upsert(zip, { ...options, api_key: 'rotated-key' }), receipt);
    assert.equal(seen[2].headers['idempotency-key'], seen[1].headers['idempotency-key']);
    assert.equal(seen[2].headers.authorization, 'Bearer rotated-key');
    assert.equal((await local.recipe_get(recipe.id)).active_version_id, receipt.recipe_version_id);
    await assert.rejects(local.recipes_upsert(zip, { ...options, virtualUserId: 'vu_other' }), /different arguments/);
  });
});
