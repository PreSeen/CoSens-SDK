import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import Database from 'better-sqlite3';
import { strToU8, zipSync } from 'fflate';
import { createServer, type IncomingHttpHeaders } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';
import test, { type TestContext } from 'node:test';
import { pathToFileURL } from 'node:url';
import { CloudClient, RSIClient, RSIError } from '../src/rsi/index.js';
import { uploadLocalRecipe } from '../src/rsi/local-recipe-upload.js';
import { Store } from '../src/rsi/store.js';
import { buildRecipePackage } from '../src/rsi/recipe-package.js';
import type { Execution, Recipe } from '../src/rsi/models.js';
import { seedRecipe } from './recipe-fixtures.js';

type Seen = {
  method: string;
  url: string;
  headers: IncomingHttpHeaders;
  body: any;
};

function setTestEnv(t: TestContext, key: string, value: string) {
  const prior = process.env[key];
  t.after(() => {
    if (prior === undefined) delete process.env[key];
    else process.env[key] = prior;
  });
  process.env[key] = value;
}

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
    const uploadOnly = new RSIClient({ data_dir: dir, config: { headers: { 'X-Test-Auth': 'fixture' } } });
    assert.equal(uploadOnly.mode, 'local');
    uploadOnly.close();
    assert.throws(() => new RSIClient({
      data_dir: dir, config: { headers: {}, auto_start: false },
    } as never), /auto_start/);
    delete process.env.COSENS_API_KEY;
    assert.throws(() => new RSIClient(), /COSENS_API_KEY/);
    const connection = { model_id: 'fixture', api_key: 'fixture-key', base_url: 'http://localhost:9/v1' };
    for (const field of Object.keys(connection)) {
      const partial = Object.fromEntries(Object.entries(connection).filter(([name]) => name !== field));
      assert.throws(() => new RSIClient({ data_dir: dir, ...partial } as never), new RegExp(field));
    }
  } finally {
    if (previous === undefined) delete process.env.COSENS_API_KEY;
    else process.env.COSENS_API_KEY = previous;
  }
});

test('cloud RSI versions the default, environment and explicit base prefixes once', async (t) => {
  const originalBase = process.env.COSENS_RSI_BASE_URL;
  t.after(() => {
    if (originalBase === undefined) delete process.env.COSENS_RSI_BASE_URL;
    else process.env.COSENS_RSI_BASE_URL = originalBase;
  });
  const seen: string[] = [];
  t.mock.method(globalThis, 'fetch', async (url: URL) => {
    seen.push(url.href);
    return Response.json({});
  });
  delete process.env.COSENS_RSI_BASE_URL;
  await new CloudClient({ api_key: 'fixture' }).execution_get('run');
  process.env.COSENS_RSI_BASE_URL = 'https://stage.example/api/';
  await new CloudClient({ api_key: 'fixture' }).execution_get('run');
  await new CloudClient({ api_key: 'fixture', base_url: 'https://direct.example/' }).execution_get('run');
  await new CloudClient({ api_key: 'fixture', base_url: 'https://stage.example/mounted/' }).execution_get('run');
  await new CloudClient({ api_key: 'fixture', base_url: 'https://stage.example/mounted/v1/' })
    .request('GET', '/v1/recipes/recipe_1/versions/version_1/download');
  for (const marker of ['?', '#']) {
    assert.throws(() => new CloudClient({ api_key: 'fixture', base_url: `https://stage.example/api${marker}` }), /base_url/);
  }
  assert.deepEqual(seen, [
    'https://friday-rsi-cn.stardust-ai.com/api/v1/executions/run', 'https://stage.example/api/v1/executions/run',
    'https://direct.example/v1/executions/run', 'https://stage.example/mounted/v1/executions/run',
    'https://stage.example/mounted/v1/recipes/recipe_1/versions/version_1/download',
  ]);
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
    assert.equal(request.url, '/api/v1/executions');
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

test('cloud create uploads user text as a replay input', async (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'rsi-cloud-'));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const inputText = '按原顺序写入 todo.md：\n确认本周销售额\n核对退款金额\n发送周报\n';
  const context = `[user] ${inputText}[assistant] Finished todo.md`;
  const contextPath = join(dir, 'context.txt');
  const artifactPath = join(dir, 'todo.md');
  writeFileSync(contextPath, context);
  writeFileSync(artifactPath, 'delivered output');

  await withService({}, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    await client.execution_create({
      evidence: {
        context_path: contextPath,
        files: [{ path: artifactPath, role: 'artifact' }],
        input_text: inputText,
      },
      idempotency_key: 'chat-delivery',
    });
    assert.equal(seen.length, 1);
    assert.deepEqual(seen[0].body, {
      idempotency_key: 'chat-delivery',
      context: { name: 'context.txt', content: context, encoding: 'utf-8' },
      files: [
        { name: 'todo.md', content: 'delivered output', encoding: 'utf-8', role: 'artifact' },
        { name: 'task-input.txt', content: inputText, encoding: 'utf-8', role: 'input' },
      ],
    });
  });
});

test('cloud reads map to routes and errors keep their codes', async () => {
  await withService(
    {
      'GET /api/v1/executions/missing': [
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
          'GET /api/v1/executions?execution_ids=a%2Cb',
          'GET /api/v1/recipes/r1?version=active',
          'PUT /api/v1/recipes/r1/versions/v1/archived',
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

test('cloud clients expose ZIP upsert without Markdown insert or local Task upload', async () => {
  await withService({}, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    const cloud = new CloudClient({ api_key: 'k', base_url });
    assert.equal('recipe_insert' in client, false);
    assert.equal('recipe_insert' in cloud, false);
    await assert.rejects(client.recipes_upsert({ task_id: 'execution_local' }), /requires a ZIP path/);
    for (const options of [{ task_id: 'execution_local' }, { local_recipe_id: 'recipe_local' }]) {
      await assert.rejects(client.recipes_upsert('unused.zip', options as never), /accepts only recipe_id/);
      await assert.rejects(cloud.recipes_upsert('unused.zip', options as never), /accepts only recipe_id/);
    }
    assert.equal(seen.length, 0);
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
  await withService({ 'POST /api/v1/recipes_upsert': [200, reply] }, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    assert.deepEqual(await client.recipes_upsert(path), reply);
    assert.deepEqual(await client.recipes_upsert(pathToFileURL(path).href, {
      recipe_id: 'recipe_1', idempotency_key: 'update-1',
    }), reply);

    assert.equal(seen.length, 2);
    for (const request of seen) {
      assert.equal(request.method, 'POST');
      assert.equal(request.url, '/api/v1/recipes_upsert');
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
    'GET /api/v1/recipes/recipe_1/versions/version_1/download': [200, zip],
    'GET /api/v1/recipes/recipe_1/versions/version_other/download': [
      404, { error: { code: 'not_found', message: 'missing version' } },
    ],
  }, async (base_url, seen) => {
    const client = new RSIClient({ mode: 'cloud', api_key: 'k', base_url });
    assert.deepEqual(await client.recipe_download('recipe_1', 'version_1'), zip);
    await assert.rejects(
      client.recipe_download('recipe_1', 'version_other'),
      (error: unknown) => error instanceof RSIError && error.code === 'not_found',
    );
    assert.equal(seen[0].url, '/api/v1/recipes/recipe_1/versions/version_1/download');
  });
});

test('local execution bootstraps a verified cloud Recipe and later publishes into it', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-cloud-base-'));
  const local = new RSIClient({
    data_dir, model_id: 'fixture', base_url: 'http://localhost:9/v1', api_key: 'fixture-model-key',
    config: {
      auto_start: false,
      headers: { Authorization: 'Bearer fixture-cosens-token', 'X-Virtual-User-Id': 'vu_fixture' },
    },
  });
  const store = new Store(data_dir);
  const originalBase = process.env.COSENS_RSI_BASE_URL;
  t.after(() => {
    if (originalBase === undefined) delete process.env.COSENS_RSI_BASE_URL;
    else process.env.COSENS_RSI_BASE_URL = originalBase;
    local.close();
    store.close();
    rmSync(data_dir, { recursive: true, force: true });
  });
  const recipeId = 'recipe_official';
  const versionId = 'version_official';
  const context_path = join(data_dir, 'context.md');
  const callerPath = join(data_dir, 'caller-RECIPE.md');
  writeFileSync(context_path, 'Update the existing Recipe');
  writeFileSync(callerPath, '---\nname: Caller\ndescription: Caller input\n---\nDifferent local file.\n');
  const markdown = '---\nname: Published\ndescription: Published Recipe\n---\nOriginal cloud instructions.\n';
  const archive = zipSync({
    'RECIPE.md': strToU8(markdown),
    'resources/helper.bin': new Uint8Array([0, 1, 2, 255]),
    'manifest.json': strToU8(JSON.stringify({ entrypoint: 'RECIPE.md', files: ['RECIPE.md', 'resources/helper.bin'] })),
  });
  const digest = createHash('sha256').update(archive).digest('hex');
  const metadata = {
    id: recipeId, archived_at: null, active_version_id: versionId,
    selected_version: {
      id: versionId, object_id: recipeId, publication_state: 'published', package_sha256: digest,
      download_path: `/v1/recipes/${recipeId}/versions/${versionId}/download`,
    },
  };
  const get = `GET /api/v1/recipes/${recipeId}`;
  const download = `GET /api/v1/recipes/${recipeId}/versions/${versionId}/download`;
  const receipt = { recipe_id: recipeId, recipe_version_id: 'version_next' };
  const replies: Record<string, [number, unknown]> = {
    [get]: [200, { ...metadata, selected_version: { ...metadata.selected_version, package_sha256: '0'.repeat(64) } }],
    [download]: [200, archive],
    'POST /api/v1/recipes_upsert': [200, receipt],
  };
  const request = {
    evidence: { context_path, files: [] },
    idempotency_key: 'rewrite-cloud-recipe',
    recipe_reference: { path: callerPath, recipe_id: recipeId, recipe_version_id: versionId },
  };

  await withService(replies, async (base_url, seen) => {
    process.env.COSENS_RSI_BASE_URL = base_url;
    await assert.rejects(local.execution_create(request), /SHA-256/);
    assert.deepEqual(await local.execution_list(), []);
    assert.deepEqual(await local.recipe_list(), []);
    replies[get] = [200, metadata];
    await assert.rejects(local.execution_create({
      ...request, recipe_reference: { ...request.recipe_reference, recipe_version_id: 'version_old' },
    }), /no longer active/);
    assert.deepEqual(await local.execution_list(), []);
    const cloud = new RSIClient({ mode: 'cloud', api_key: 'fixture', base_url });
    await assert.rejects(cloud.execution_create(request), /requires local mode/);

    const [first, duplicate] = await Promise.all([local.execution_create(request), local.execution_create(request)]);
    assert.equal(duplicate.id, first.id);
    assert.deepEqual(await local.execution_list().then((rows) => rows.map(({ id }) => id)), [first.id]);
    const localVersion = await local.recipe_get(recipeId, versionId);
    assert.equal(localVersion.id, recipeId);
    assert.equal(localVersion.active_version_id, versionId);
    assert.equal(localVersion.selected_version?.id, versionId);
    const installed = join(data_dir, localVersion.storage_path, versionId);
    assert.equal(readFileSync(join(installed, 'RECIPE.md'), 'utf8'), markdown);
    assert.deepEqual(readFileSync(join(installed, 'resources/helper.bin')), Buffer.from([0, 1, 2, 255]));
    const enteredRecipe = first.evidence.find(({ role }) => role === 'recipe');
    assert.equal(enteredRecipe?.path, realpathSync(join(installed, 'RECIPE.md')));
    assert.equal(store.get<Execution>(first.id, 'execution').base_recipe_version_id, versionId);
    for (const request of seen) {
      assert.equal(request.headers.authorization, 'Bearer fixture-cosens-token');
      assert.equal(request.headers['x-virtual-user-id'], 'vu_fixture');
    }
    assert.ok(seen.some(({ method, url }) => method === 'GET' && url.includes(`?version=${versionId}`)));

    const count = seen.length;
    const originalZip = join(data_dir, 'original.zip');
    writeFileSync(originalZip, archive);
    await assert.rejects(uploadLocalRecipe(store, originalZip, {
      base_url, local_recipe_id: recipeId, local_recipe_version_id: versionId,
    }), /already published in the cloud/);
    process.env.COSENS_RSI_BASE_URL = 'http://127.0.0.1:1/api';
    assert.equal((await local.execution_create(request)).id, first.id);
    const offline = await local.execution_create({
      ...request, idempotency_key: 'offline-cloud-recipe', recipe_reference: { path: callerPath, recipe_id: recipeId },
    });
    assert.notEqual(offline.id, first.id);
    assert.equal(store.get<Execution>(offline.id, 'execution').base_recipe_version_id, versionId);
    assert.equal(seen.length, count);
    await local.execution_cancel(offline.id);

    store.tx(() => {
      const version = store.append(store.get<Recipe>(recipeId, 'recipe'), {
        name: 'Published', description: 'Updated Recipe', instructions: 'Revised instructions.',
      }, undefined, { execution_id: first.id });
      store.publishRecipeVersion(recipeId, version.id);
      store.save<Execution>({
        ...store.get<Execution>(first.id, 'execution'), status: 'completed', recipe_version_id: version.id,
        result: { test_status: 'passed', published: true, provisional: false },
      });
    });
    const task = await local.task_get(first.id);
    assert.ok(task.artifact);
    const { recipe_id, recipe_version_id } = task;
    assert.equal(recipe_id, recipeId);
    assert.ok(recipe_version_id);
    assert.equal(recipe_version_id, task.artifact.recipe_version_id);
    assert.equal(task.recipe_zip_path, task.artifact.zip_path);
    process.env.COSENS_RSI_BASE_URL = base_url;
    assert.deepEqual(await local.recipes_upsert({ task_id: task.task_id }), receipt);
    assert.equal(seen.at(-1)?.body.get('recipe_id'), recipeId);
    assert.equal((await local.task_get(first.id)).recipe_version_id, receipt.recipe_version_id);

    const draft = store.tx(() => store.append(store.get<Recipe>(recipeId, 'recipe'), {
      name: 'Local draft', description: 'Unpublished draft', instructions: 'Keep local changes.',
    }));
    process.env.COSENS_RSI_BASE_URL = base_url;
    const otherVersion = 'version_other_device';
    replies[get] = [200, {
      ...metadata, active_version_id: otherVersion,
      selected_version: { ...metadata.selected_version, id: otherVersion },
    }];
    replies[`GET /api/v1/recipes/${recipeId}/versions/${otherVersion}/download`] = [200, archive];
    const otherRequest = {
      ...request, idempotency_key: 'cloud-version-from-other-device',
      recipe_reference: { ...request.recipe_reference, recipe_version_id: otherVersion },
    };
    store.remember('recipe_cloud_upload_pending', recipeId, { cloud_recipe_id: recipeId }, {});
    await assert.rejects(local.execution_create(otherRequest), /pending local upload/);
    store.db.prepare("DELETE FROM idempotency WHERE scope='recipe_cloud_upload_pending' AND key=?").run(recipeId);
    const otherDevice = await local.execution_create(otherRequest);
    assert.equal(store.get<Execution>(otherDevice.id, 'execution').base_recipe_version_id, otherVersion);
    assert.equal((await local.recipe_get(recipeId)).active_version_id, receipt.recipe_version_id);
    assert.equal((await local.recipe_get(recipeId, draft.id)).selected_version?.content.name, 'Local draft');
    const native = await seedRecipe(data_dir, callerPath);
    const beforeMissing = seen.length;
    await assert.rejects(local.execution_create({
      ...request, idempotency_key: 'missing-native-version',
      recipe_reference: { path: callerPath, recipe_id: native.id, recipe_version_id: 'version_missing' },
    }), /local base Recipe version not found/);
    assert.equal(seen.length, beforeMissing);
  });
});


test('local upsert selects the exact Task ZIP and replays even when its ZIP is pruned', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-local-zip-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const path = join(data_dir, 'RECIPE.md');
  const zip = join(data_dir, 'recipe.zip');
  const firstMarkdown = '---\nname: Report\ndescription: Generate the report\n---\nDraft one\n';
  writeFileSync(path, firstMarkdown);
  const headers = {
    Authorization: 'Bearer fixture-access-token',
    'X-Auth-Type': 'access_token',
    'X-Virtual-User-Id': 'vu_fixture',
    'Content-Type': 'application/json',
    'Content-Length': '0',
    'Idempotency-Key': 'model-key',
  };
  const localOptions = {
    data_dir,
    model_id: 'fixture', base_url: 'http://localhost:9/v1', api_key: 'fixture-model-key',
    config: { auto_start: false, headers },
  };
  const local = new RSIClient(localOptions);
  const otherTenant = new RSIClient({
    ...localOptions, config: { ...localOptions.config, headers: { ...headers, 'X-Virtual-User-Id': 'vu_other' } },
  });
  const uploadStore = new Store(data_dir);
  const uploadVersion = (path: string, options: Parameters<typeof uploadLocalRecipe>[2]) =>
    uploadLocalRecipe(uploadStore, path, options, new Headers(headers));
  t.after(() => { local.close(); otherTenant.close(); uploadStore.close(); });
  const recipe = await seedRecipe(data_dir, path);
  const firstLocalVersion = recipe.active_version_id!;
  const firstDirectory = join(data_dir, recipe.storage_path, firstLocalVersion);
  writeFileSync(join(firstDirectory, 'helper.py'), 'print(42)\n');
  writeFileSync(zip, buildRecipePackage(firstDirectory));
  const contextPath = join(data_dir, 'context.md');
  writeFileSync(contextPath, 'Example task context');
  const execution = await local.execution_create({
    evidence: { context_path: contextPath, files: [] },
    idempotency_key: 'recipe-upload-execution',
    recipe_reference: { path, recipe_id: recipe.id },
  });
  const first = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud_1' };
  const second = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud_2' };
  const replies: Record<string, [number, unknown]> = { 'POST /api/v1/recipes_upsert': [200, first] };
  await withService(replies, async (base_url, seen) => {
    setTestEnv(t, 'COSENS_RSI_BASE_URL', base_url);
    setTestEnv(t, 'COSENS_API_KEY', 'fixture-ambient-key');
    const initial = { base_url, local_recipe_id: recipe.id, local_recipe_version_id: firstLocalVersion };
    const selectedUpload = { task_id: execution.id };
    const priorPair = { recipe_id: recipe.id, recipe_version_id: firstLocalVersion };
    await assert.rejects(local.recipes_upsert(zip), /requires \{ task_id \}/);
    await assert.rejects(local.recipes_upsert(zip, initial as never), /requires \{ task_id \}/);
    await assert.rejects(local.recipes_upsert(priorPair as never), /Unrecognized key/);
    await assert.rejects(local.recipes_upsert({ ...selectedUpload, ...priorPair } as never), /Unrecognized key/);
    await assert.rejects(local.recipes_upsert({ ...selectedUpload, local_recipe_id: recipe.id } as never), /Unrecognized key/);
    for (const removedOptions of [
      { base_url }, { api_key: 'old-key' }, { virtualUserId: 'vu_override' },
    ]) {
      await assert.rejects(local.recipes_upsert({ ...selectedUpload, ...removedOptions } as never), /Unrecognized key/);
    }
    await assert.rejects(local.recipes_upsert(selectedUpload), /has no approved Recipe version/);
    await assert.rejects(uploadVersion(zip, initial), /finish the pending Recipe execution/);
    assert.equal(seen.length, 0);
    const setup = new Database(join(data_dir, 'rsi-ts.sqlite3'));
    try {
      setup.prepare(`UPDATE executions SET status='completed', recipe_version_id=?,
        details=json_set(details, '$.test_results', json(?), '$.result', json(?)) WHERE id=?`)
        .run(firstLocalVersion, JSON.stringify([{
          id: 'test-persisted', recipe_id: recipe.id, version_id: firstLocalVersion,
        }]), JSON.stringify({ test_status: 'passed', published: true, provisional: false }), execution.id);
      setup.prepare('INSERT INTO idempotency VALUES (?,?,?,?)').run(
        'recipe_retry', 'old-retry-key', JSON.stringify({ recipe_id: recipe.id }),
        JSON.stringify({ id: execution.id, fence: 0 }),
      );
    } finally {
      setup.close();
    }
    const secondLocalVersion = uploadStore.tx(() => {
      const version = uploadStore.append(uploadStore.get<Recipe>(recipe.id, 'recipe'), {
        name: 'Report', description: 'Generate the report', instructions: 'Draft two',
      });
      uploadStore.publishRecipeVersion(recipe.id, version.id);
      return version.id;
    });
    assert.equal((await local.recipe_get(recipe.id)).active_version_id, secondLocalVersion);
    const packages = join(data_dir, 'task-packages');
    mkdirSync(packages);
    writeFileSync(join(packages, `${execution.id}.zip`), 'orphan from interrupted commit');
    const task = await local.task_get(execution.id);
    assert.equal(task.recipe_id, recipe.id);
    assert.equal(task.recipe_version_id, firstLocalVersion);
    assert.ok(task.artifact);
    assert.equal(task.artifact.recipe_id, recipe.id);
    assert.equal(task.artifact.recipe_version_id, firstLocalVersion);
    assert.equal(task.recipe_zip_path, task.artifact.zip_path);
    assert.ok(isAbsolute(task.artifact.zip_path));
    assert.deepEqual(readFileSync(task.artifact.zip_path), readFileSync(zip));
    const original = readFileSync(zip);
    const repacked = Buffer.concat([original, Buffer.from('x')]);
    repacked.writeUInt16LE(1, original.length - 2);
    const repackedZip = join(data_dir, 'repacked.zip');
    writeFileSync(repackedZip, repacked);
    await assert.rejects(uploadVersion(repackedZip, initial), /frozen Task Recipe ZIP/);
    assert.equal(seen.length, 0);
    replies['POST /api/v1/recipes_upsert'] = [503, { error: { code: 'unavailable', message: 'retry Task upload' } }];
    await assert.rejects(local.recipes_upsert(selectedUpload), /retry Task upload/);
    await assert.rejects(otherTenant.recipes_upsert(selectedUpload), /another upload is pending/);
    assert.equal(seen.length, 1);
    const pendingKey = seen[0].headers['idempotency-key'];
    uploadStore.db.prepare("UPDATE idempotency SET request=json_set(request, '$.base_url', ?) WHERE scope=?")
      .run(base_url, 'recipe_cloud_upload_pending');
    seen.length = 0;
    rmSync(task.artifact.zip_path);
    replies['POST /api/v1/recipes_upsert'] = [200, first];
    const uploadOnly = new RSIClient({ data_dir, config: { headers } });
    try {
      assert.deepEqual(await uploadOnly.recipes_upsert(selectedUpload), first);
    } finally {
      uploadOnly.close();
    }
    assert.equal(seen[0].headers['idempotency-key'], pendingKey);
    assert.deepEqual(await local.recipes_upsert(selectedUpload), first);
    assert.deepEqual(await local.recipes_upsert({ ...selectedUpload, recipe_id: first.recipe_id }), first);
    await assert.rejects(otherTenant.recipes_upsert(selectedUpload), /different arguments/);
    assert.equal(seen[0].body.get('recipe_id'), null);
    assert.deepEqual(Buffer.from(await seen[0].body.get('file').arrayBuffer()), readFileSync(zip));
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
    const syncedTask = await local.task_get(execution.id);
    assert.deepEqual(syncedTask.artifact, { zip_path: task.artifact.zip_path, ...first });
    assert.equal(syncedTask.recipe_id, first.recipe_id);
    assert.equal(syncedTask.recipe_version_id, first.recipe_version_id);
    assert.equal(syncedTask.recipe_zip_path, task.artifact.zip_path);
    assert.deepEqual(resumed.test_results, [{
      id: 'test-persisted', recipe_id: first.recipe_id, version_id: first.recipe_version_id,
    }]);
    const listed = await local.recipe_list({ recipe_ids: [recipe.id, first.recipe_id] });
    assert.equal(listed.length, 1);
    assert.equal(listed[0].recipe_id, first.recipe_id);
    assert.deepEqual(await local.recover({ recipe_id: first.recipe_id, retry_key: 'old-retry-key' }), {
      started: 0, recipe_id: first.recipe_id, execution_id: execution.id,
    });

    const nextZip = join(data_dir, 'recipe-v2.zip');
    uploadStore.db.prepare("UPDATE idempotency SET request=json_set(request, '$.base_url', ?) WHERE scope IN (?,?)")
      .run(base_url, 'recipe_cloud_upload_receipt', 'recipe_cloud_identity');
    writeFileSync(nextZip, buildRecipePackage(join(data_dir, recipe.storage_path, secondLocalVersion)));
    replies['POST /api/v1/recipes_upsert'] = [200, second];
    assert.deepEqual(await uploadVersion(nextZip, {
      ...initial, local_recipe_version_id: secondLocalVersion,
    }), second);
    assert.equal(seen[1].body.get('recipe_id'), first.recipe_id);
    assert.deepEqual(await uploadVersion(zip, initial), first);
    assert.deepEqual(await uploadVersion(zip, {
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
      assert.deepEqual(db.prepare('SELECT id,recipe_id,recipe_version_id FROM recipes').all(), [{
        id: execution.id, recipe_id: first.recipe_id, recipe_version_id: first.recipe_version_id,
      }]);
      assert.deepEqual(db.prepare('SELECT id FROM versions WHERE recipe_id=? ORDER BY number').all(first.recipe_id), [
        { id: first.recipe_version_id }, { id: second.recipe_version_id },
      ]);
      assert.deepEqual(db.prepare('SELECT id,recipe_id,recipe_version_id FROM executions').get(), {
        id: execution.id, recipe_id: first.recipe_id, recipe_version_id: first.recipe_version_id,
      });
      assert.deepEqual(db.pragma('foreign_key_check'), []);
      assert.equal(db.prepare('SELECT 1 FROM idempotency WHERE scope=?').get('recipe_cloud_upload_pending'), undefined);
      assert.doesNotMatch(JSON.stringify(db.prepare('SELECT request,result FROM idempotency').all()), /fixture-access-token/);
    } finally {
      db.close();
    }
    rmSync(task.artifact.zip_path);
    rmSync(join(data_dir, synced.storage_path, first.recipe_version_id), { recursive: true });
    const prunedTask = await local.task_get(execution.id);
    assert.equal(prunedTask.recipe_id, first.recipe_id);
    assert.equal(prunedTask.recipe_version_id, first.recipe_version_id);
    assert.equal(prunedTask.recipe_zip_path, null);
    assert.equal(prunedTask.artifact, null);
    assert.deepEqual(await local.recipes_upsert(selectedUpload), first);
    await assert.rejects(local.recipes_upsert({ ...first, base_url } as never), /Unrecognized key/);
    await assert.rejects(local.recipes_upsert({ ...selectedUpload, idempotency_key: 'changed-key' }), /different arguments/);
    process.env.COSENS_RSI_BASE_URL = base_url + '/other';
    await assert.rejects(local.recipes_upsert(selectedUpload), /different arguments/);
    process.env.COSENS_RSI_BASE_URL = base_url;
    assert.equal(seen.length, 2);
    const alias = await seedRecipe(data_dir, path);
    await assert.rejects(uploadVersion(zip, { ...initial, local_recipe_id: alias.id }), /version does not belong/);
    assert.equal(seen.length, 2);
    const aliasZip = join(data_dir, 'alias.zip');
    writeFileSync(aliasZip, buildRecipePackage(join(data_dir, alias.storage_path, alias.active_version_id!)));
    await assert.rejects(uploadVersion(aliasZip, {
      ...initial, local_recipe_id: alias.id, local_recipe_version_id: alias.active_version_id!,
    }), /already used locally/);
    assert.equal((await local.recipe_get(alias.id)).id, alias.id);
  });
});

test('local publication can bind an explicit cloud target and safely resume that reservation', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-explicit-target-'));
  const client = new RSIClient({
    data_dir, model_id: 'fixture', base_url: 'http://localhost:9/v1', api_key: 'fixture-model-key',
    config: { auto_start: false, headers: { Authorization: 'Bearer fixture-access-token' } },
  });
  const store = new Store(data_dir);
  t.after(() => { client.close(); store.close(); rmSync(data_dir, { recursive: true, force: true }); });
  const context_path = join(data_dir, 'context.md');
  writeFileSync(context_path, 'Task context.');
  const resourceBytes = Buffer.from([0, 255, 42]);
  const approve = async (
    idempotency_key: string, recipe_reference?: { path: string; recipe_id: string }, reuseVersion?: string,
  ) => {
    const execution = await client.execution_create({ evidence: { context_path, files: [] }, idempotency_key, recipe_reference });
    store.tx(() => {
      const recipe = store.get<Recipe>(execution.recipe_id!, 'recipe');
      const version = reuseVersion ? store.version(recipe.id, reuseVersion)
        : store.append(recipe, { name: 'Approved', description: 'Fixture', instructions: 'Write output.' });
      if (!reuseVersion) {
        const resources = join(data_dir, recipe.storage_path, version.id, 'resources');
        mkdirSync(resources);
        writeFileSync(join(resources, 'data.bin'), resourceBytes);
      }
      store.publishRecipeVersion(recipe.id, version.id);
      store.save<Execution>({
        ...store.get<Execution>(execution.id, 'execution'), status: 'completed', recipe_version_id: version.id,
        result: { test_status: 'passed', published: true, provisional: false },
      });
    });
    const task = await client.task_get(execution.id);
    assert.ok(task.recipe_id && task.recipe_version_id && task.recipe_zip_path);
    return task;
  };

  const first = await approve('first');
  const other = await approve('other');
  const target = 'recipe_shared_cloud';
  const receipt = { recipe_id: target, recipe_version_id: 'version_bound_cloud' };
  const endpoint = 'POST /api/v1/recipes_upsert';
  const replies: Record<string, [number, unknown]> = {
    [endpoint]: [503, { error: { code: 'unavailable', message: 'retry the target' } }],
  };
  await withService(replies, async (base_url, seen) => {
    setTestEnv(t, 'COSENS_RSI_BASE_URL', base_url);
    const upload = { task_id: first.task_id };
    await assert.rejects(client.recipes_upsert({ ...upload, recipe_id: target }), /retry the target/);
    assert.equal(seen[0].body.get('recipe_id'), target);
    const sha = createHash('sha256').update(readFileSync(first.recipe_zip_path!)).digest('hex');
    const key = createHash('sha256').update([first.recipe_id, first.recipe_version_id, sha, target].join('\0')).digest('hex');
    assert.equal(seen[0].headers['idempotency-key'], `local-recipe-upload-${key}`);
    await assert.rejects(client.recipes_upsert({ task_id: other.task_id, recipe_id: target }), /pending local upload/);
    await assert.rejects(client.recipes_upsert({ ...upload, recipe_id: 'recipe_different' }), /another upload is pending/);
    assert.equal(seen.length, 1);

    replies[endpoint] = [200, { ...receipt, recipe_id: 'recipe_wrong_receipt' }];
    await assert.rejects(client.recipes_upsert(upload), /mismatched or invalid IDs/);
    assert.equal(seen[1].body.get('recipe_id'), target);
    assert.equal(seen[1].headers['idempotency-key'], seen[0].headers['idempotency-key']);
    replies[endpoint] = [200, receipt];
    assert.deepEqual(await client.recipes_upsert(upload), receipt);
    assert.equal(seen[2].body.get('recipe_id'), target);
    assert.equal(seen[2].headers['idempotency-key'], seen[0].headers['idempotency-key']);
    const synced = await client.task_get(first.task_id);
    assert.equal(synced.task_id, first.task_id);
    assert.equal(synced.recipe_id, target);
    assert.equal(synced.recipe_version_id, receipt.recipe_version_id);
    assert.equal(synced.recipe_zip_path, first.recipe_zip_path);
    assert.deepEqual(await client.recipes_upsert({ ...upload, recipe_id: target }), receipt);
    await assert.rejects(client.recipes_upsert({ ...upload, recipe_id: 'recipe_different' }), /different arguments/);
    await assert.rejects(client.recipes_upsert({ task_id: other.task_id, recipe_id: first.recipe_id! }), /already used locally/);
    assert.equal(seen.length, 3);

    const recipe = await client.recipe_get(target);
    const path = join(data_dir, recipe.storage_path, receipt.recipe_version_id, 'RECIPE.md');
    const next = await approve('next', { path, recipe_id: target });
    await assert.rejects(client.recipes_upsert({
      task_id: next.task_id, recipe_id: 'recipe_different',
    }), /already linked to a different cloud Recipe/);
    assert.equal(seen.length, 3);
    const nextReceipt = { recipe_id: target, recipe_version_id: 'version_next_cloud' };
    replies[endpoint] = [200, nextReceipt];
    assert.deepEqual(await client.recipes_upsert({ task_id: next.task_id }), nextReceipt);
    assert.equal(seen[3].body.get('recipe_id'), target);

    const original = await client.recipe_get(other.recipe_id!);
    const sibling = await approve('sibling', {
      recipe_id: original.id, path: join(data_dir, original.storage_path, other.recipe_version_id!, 'RECIPE.md'),
    }, other.recipe_version_id!);
    const draft = store.tx(() => store.append(store.get<Recipe>(target, 'recipe'), {
      name: 'Draft', description: 'Fixture', instructions: 'Keep this draft.',
    }));
    const link = { task_id: other.task_id, recipe_id: target };
    const linked = { recipe_id: target, recipe_version_id: 'version_cross_task' };
    const differentTenant = new RSIClient({
      data_dir,
      config: { headers: { Authorization: 'Bearer fixture-access-token', 'X-Virtual-User-Id': 'different' } },
    });
    try {
      await assert.rejects(differentTenant.recipes_upsert(link), /target Recipe was synchronized to a different/);
    } finally {
      differentTenant.close();
    }
    assert.equal(seen.length, 4);
    replies[endpoint] = [503, { error: { code: 'unavailable', message: 'retry the association' } }];
    await assert.rejects(client.recipes_upsert(link), /retry the association/);
    await assert.rejects(client.recipes_upsert({ task_id: sibling.task_id, recipe_id: target }), /another upload is pending/);
    const frozen = readFileSync(other.recipe_zip_path!);
    const digest = createHash('sha256').update(frozen).digest('hex');
    const scopedKey = createHash('sha256')
      .update([other.recipe_id, other.recipe_version_id, digest, target, other.task_id].join('\0')).digest('hex');
    const linkedKey = `local-recipe-upload-${scopedKey}`;
    assert.equal(seen[4].headers['idempotency-key'], linkedKey);
    replies[endpoint] = [200, linked];
    assert.deepEqual(await Promise.all([
      client.recipes_upsert({ task_id: other.task_id }), client.recipes_upsert(link),
    ]), [linked, linked]);
    assert.equal(seen[5].body.get('recipe_id'), target);
    assert.deepEqual(Buffer.from(await seen[5].body.get('file').arrayBuffer()), frozen);
    const targetRecipe = await client.recipe_get(target);
    assert.equal(targetRecipe.active_version_id, linked.recipe_version_id);
    assert.deepEqual(readFileSync(join(data_dir, targetRecipe.storage_path, linked.recipe_version_id, 'resources/data.bin')), resourceBytes);
    assert.deepEqual(store.db.prepare('SELECT status FROM versions WHERE id=?').get(draft.id), { status: 'candidate' });
    const originalExecution = await client.execution_get(other.execution_id);
    assert.equal(originalExecution.recipe_id, other.recipe_id);
    assert.equal(originalExecution.recipe_version_id, other.recipe_version_id);
    assert.equal((await client.task_get(sibling.task_id)).recipe_id, sibling.recipe_id);
    assert.deepEqual((await client.task_get(other.task_id)).artifact, { ...linked, zip_path: other.recipe_zip_path });
    const linkedRequestCount = seen.length;
    await assert.rejects(client.recipes_upsert({
      task_id: sibling.task_id, recipe_id: target, idempotency_key: linkedKey,
    }), /idempotency key is already used/);
    assert.equal(seen.length, linkedRequestCount);

    const siblingReceipt = { recipe_id: 'recipe_independent', recipe_version_id: 'version_independent' };
    replies[endpoint] = [200, siblingReceipt];
    assert.deepEqual(await client.recipes_upsert({ task_id: sibling.task_id }), siblingReceipt);
    assert.equal((await client.task_get(sibling.task_id)).recipe_id, siblingReceipt.recipe_id);
    assert.deepEqual((await client.task_get(other.task_id)).artifact, { ...linked, zip_path: other.recipe_zip_path });
    const rekeyedOriginal = await client.recipe_get(siblingReceipt.recipe_id);
    rmSync(other.recipe_zip_path!);
    rmSync(join(data_dir, rekeyedOriginal.storage_path, siblingReceipt.recipe_version_id), { recursive: true });
    const pruned = await client.task_get(other.task_id);
    assert.equal(pruned.recipe_id, linked.recipe_id);
    assert.equal(pruned.recipe_version_id, linked.recipe_version_id);
    assert.equal(pruned.recipe_zip_path, null);
    assert.deepEqual(store.db.prepare('SELECT recipe_id,recipe_version_id,recipe_zip_path FROM recipes WHERE id=?').get(other.task_id), {
      ...linked, recipe_zip_path: null,
    });
    const offline = new RSIClient({ data_dir, config: null });
    const totalRequests = seen.length;
    try {
      assert.deepEqual(await offline.recipes_upsert(link), linked);
      await assert.rejects(offline.recipes_upsert({ ...link, recipe_id: siblingReceipt.recipe_id }), /different arguments/);
      await assert.rejects(offline.recipes_upsert({ ...link, idempotency_key: 'different' }), /different arguments/);
    } finally {
      offline.close();
    }
    assert.equal(seen.length, totalRequests);
  });
});

test('failed local ZIP upload retains a retry only for the same bytes and destination', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-local-zip-retry-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const path = join(data_dir, 'RECIPE.md');
  const zip = join(data_dir, 'recipe.zip');
  const alternate = join(data_dir, 'alternate.zip');
  const mismatch = join(data_dir, 'mismatch.zip');
  const markdown = '---\nname: Report\ndescription: Generate the report\n---\nMake a report.\n';
  writeFileSync(path, markdown);
  const local = new RSIClient({ data_dir, config: null });
  const store = new Store(data_dir);
  t.after(() => { local.close(); store.close(); });
  const recipe = await seedRecipe(data_dir, path);
  writeFileSync(zip, buildRecipePackage(join(data_dir, recipe.storage_path, recipe.active_version_id!)));
  const archive = (content: string) => zipSync({
    'RECIPE.md': strToU8(content),
    'manifest.json': strToU8(JSON.stringify({ entrypoint: 'RECIPE.md', files: ['RECIPE.md'] })),
  }, { level: 0 });
  writeFileSync(alternate, archive(markdown));
  writeFileSync(mismatch, archive(markdown + 'Wrong version.\n'));
  const replies: Record<string, [number, unknown]> = {
    'POST /api/v1/recipes_upsert': [422, { error: { code: 'invalid_request', message: 'ZIP invalid' } }],
  };
  await withService(replies, async (base_url, seen) => {
    const options = { base_url, api_key: 'k', local_recipe_id: recipe.id, local_recipe_version_id: recipe.active_version_id! };
    await assert.rejects(uploadLocalRecipe(store, mismatch, options), /match/i);
    assert.equal(seen.length, 0);
    await assert.rejects(uploadLocalRecipe(store, alternate, options), /ZIP invalid/);
    replies['POST /api/v1/recipes_upsert'] = [503, { error: { code: 'unavailable', message: 'try again' } }];
    await assert.rejects(uploadLocalRecipe(store, zip, options), /try again/);
    assert.equal((await local.recipe_get(recipe.id)).id, recipe.id);
    await assert.rejects(uploadLocalRecipe(store, alternate, options), /another upload is pending/);
    await assert.rejects(uploadLocalRecipe(store, zip, { ...options, virtualUserId: 'vu_other' }), /another upload is pending/);
    await assert.rejects(uploadLocalRecipe(store, zip, { ...options, base_url: base_url + '/other' }), /another upload is pending/);
    await assert.rejects(uploadLocalRecipe(store, zip, { ...options, local_recipe_version_id: 'version_missing' }), /version does not belong/);
    assert.equal(seen.length, 2);
    const receipt = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud' };
    replies['POST /api/v1/recipes_upsert'] = [200, receipt];
    assert.deepEqual(await uploadLocalRecipe(store, zip, { ...options, api_key: 'rotated-key' }), receipt);
    assert.equal(seen[2].headers['idempotency-key'], seen[1].headers['idempotency-key']);
    assert.equal(seen[2].headers.authorization, 'Bearer rotated-key');
    assert.equal((await local.recipe_get(recipe.id)).active_version_id, receipt.recipe_version_id);
    await assert.rejects(uploadLocalRecipe(store, zip, { ...options, virtualUserId: 'vu_other' }), /different arguments/);
  });
});

test('local upload rejects candidates and a legacy approved version retains its cloud receipt', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-local-candidate-'));
  const local = new RSIClient({
    data_dir, model_id: 'fixture', base_url: 'http://localhost:9/v1', api_key: 'fixture',
    config: { auto_start: false },
  });
  const store = new Store(data_dir);
  t.after(() => {
    store.close();
    local.close();
    rmSync(data_dir, { recursive: true, force: true });
  });
  const context_path = join(data_dir, 'context.md');
  writeFileSync(context_path, 'A task');
  const run = await local.execution_create({ evidence: { context_path, files: [] }, idempotency_key: 'candidate' });
  const recipe = store.get<Recipe>(run.recipe_id!, 'recipe');
  const version = store.tx(() => {
    const candidate = store.append(recipe, {
      name: 'Candidate', description: 'Draft for testing', instructions: 'Draft.',
    }, undefined, { execution_id: run.id });
    store.save<Execution>({
      ...store.get<Execution>(run.id, 'execution'),
      status: 'completed', recipe_version_id: candidate.id,
      result: { test_status: 'passed', provisional: true, published: false },
    });
    return candidate;
  });
  const zip = join(data_dir, 'candidate.zip');
  writeFileSync(zip, buildRecipePackage(join(data_dir, recipe.storage_path, version.id)));
  const provisional = await local.task_get(run.id);
  assert.equal(provisional.recipe_id, recipe.id);
  assert.equal(provisional.recipe_version_id, null);
  assert.equal(provisional.recipe_zip_path, null);
  assert.equal(provisional.artifact, null);
  await withService({}, async (base_url, seen) => {
    await assert.rejects(local.recipes_upsert({ task_id: run.id }), /has no approved Recipe version/);
    await assert.rejects(uploadLocalRecipe(store, zip, {
      base_url, api_key: 'k', local_recipe_id: recipe.id, local_recipe_version_id: version.id,
    }), /must be approved/);
    assert.equal(seen.length, 0);
  });
  store.tx(() => {
    store.publishRecipeVersion(recipe.id, version.id);
    store.save<Execution>({
      ...store.get<Execution>(run.id, 'execution'),
      result: { test_status: 'passed', provisional: false, published: true },
    });
  });
  const original = readFileSync(zip);
  const legacy = Buffer.concat([original, Buffer.from('x')]);
  legacy.writeUInt16LE(1, original.length - 2);
  writeFileSync(zip, legacy);
  const receipt = { recipe_id: 'recipe_cloud', recipe_version_id: 'version_cloud' };
  await withService({ 'POST /api/v1/recipes_upsert': [200, receipt] }, async (base_url, seen) => {
    setTestEnv(t, 'COSENS_RSI_BASE_URL', base_url);
    setTestEnv(t, 'COSENS_API_KEY', 'fixture-ambient-key');
    await assert.rejects(local.recipes_upsert({ task_id: run.id }), /config.headers/);
    assert.equal(seen.length, 0);
    assert.equal(store.db.prepare('SELECT 1 FROM idempotency WHERE scope=?')
      .get('recipe_cloud_upload_pending'), undefined);
    await uploadLocalRecipe(store, zip, {
      base_url, api_key: 'k', local_recipe_id: recipe.id, local_recipe_version_id: version.id,
    });
    delete process.env.COSENS_API_KEY;
    assert.deepEqual(await local.recipes_upsert({ task_id: run.id }), receipt);
    assert.equal(seen.length, 1);
  });
  const synced = await local.task_get(run.id);
  assert.equal(synced.recipe_id, receipt.recipe_id);
  assert.equal(synced.recipe_version_id, receipt.recipe_version_id);
  assert.equal(synced.recipe_zip_path, null);
  assert.equal(synced.artifact, null);
});
