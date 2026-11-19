import assert from 'node:assert/strict';
import { once } from 'node:events';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { createServer, type IncomingHttpHeaders } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
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
    let text = '';
    request.on('data', (chunk) => (text += chunk));
    request.on('end', () => {
      seen.push({
        method: request.method!,
        url: request.url!,
        headers: request.headers,
        body: text ? JSON.parse(text) : undefined,
      });
      const path = request.url!.split('?')[0];
      const [status, payload] = replies[`${request.method} ${path}`] ?? [
        200,
        { ok: true },
      ];
      response.statusCode = status;
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
      ['sales.csv', 'chart.png'],
    );
    assert.equal(request.body.files[1].encoding, 'base64');
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
      await assert.rejects(
        client.execution_get('missing'),
        (error: unknown) =>
          error instanceof RSIError && error.code === 'not_found',
      );
    },
  );
});
