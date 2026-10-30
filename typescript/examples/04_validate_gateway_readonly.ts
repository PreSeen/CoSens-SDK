import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { CoSensClient } from '../src/index.ts';

const apiKey = process.env.RSI_API_KEY ?? process.env.COSENS_API_KEY;
if (!apiKey) throw new Error('Set RSI_API_KEY or COSENS_API_KEY before gateway validation');
const outputIndex = process.argv.indexOf('--output');
const outputPath = outputIndex >= 0 ? process.argv[outputIndex + 1] : undefined;
if (outputIndex >= 0 && !outputPath) throw new Error('--output requires a file path');

const observed: Array<{ method: string; path: string; status: number }> = [];
const checkedFetch: typeof fetch = async (input, init) => {
  const headers = new Headers(init?.headers);
  assert.equal(headers.get('x-auth-type'), 'api_key');
  assert.equal(headers.get('authorization'), `Bearer ${apiKey}`);
  assert.equal(headers.has('x-user-id') || headers.has('x_user_id'), false);
  const url = new URL(String(input));
  assert.equal(url.protocol, 'https:');
  assert.ok(url.pathname.startsWith('/api/'));
  const response = await fetch(input, init);
  observed.push({ method: init?.method ?? 'GET', path: url.pathname + url.search, status: response.status });
  return response;
};

const client = new CoSensClient({ apiKey, fetch: checkedFetch });
try {
  const ready = await client.transport.request('GET', '/health/ready');
  assert.equal(ready.status, 'ok');
  const openapi = await client.transport.request('GET', '/openapi.json');
  assert.equal(typeof openapi?.info?.version, 'string');
  const context = await client.atoms.recallContext({ query: 'CoSens TypeScript SDK gateway validation', tokenBudget: 200 });
  assert.equal(context.run_id, undefined);
  const items = await client.atoms.getMemoryItems({ limit: 1 });
  assert.ok(Array.isArray(items.sources) && Array.isArray(items.experiences));
  assert.equal(items.limit, 1);
  const report = { region: client.transport.service.region, gateway: client.transport.service.url, openapiVersion: openapi.info.version, observed, standaloneRecall: 'passed', memoryItems: 'passed' };
  if (outputPath) {
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify({ ...report, outputPath: outputPath ?? null }));
} finally {
  await client.close();
}
