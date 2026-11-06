import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensClient, TurnStore } from '../src/index.ts';

test('default and explicit US clients use the public API host', async () => {
  const hosts: string[] = [];
  const fetcher: typeof fetch = async input => {
    hosts.push(new URL(String(input)).host);
    return new Response(JSON.stringify({ run_id: 'r1', status: 'ready' }));
  };
  const defaultClient = new CoSensClient({ apiKey: 'key', fetch: fetcher });
  const us = new CoSensClient({ apiKey: 'key', region: 'us', fetch: fetcher });
  await defaultClient.atoms.getTask('r1');
  await us.atoms.getTask('r1');
  assert.deepEqual(hosts, ['rsi.cosens.ai', 'rsi.cosens.ai']);
  await defaultClient.close(); await us.close();
});

test('turn state is isolated by service identity even for the same API Key', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'cosens-region-')), 'state.sqlite3');
  const old = new TurnStore(path, 'key', 'old-service');
  const us = new TurnStore(path, 'key', 'rsi.cosens.ai/api');
  old.put('turn-1', { state: 'completed' });
  assert.equal(us.get('turn-1'), undefined);
  us.put('turn-1', { state: 'pre_execution_pending' });
  assert.equal(old.get('turn-1')?.state, 'completed');
  old.close(); us.close();
});

test('invalid region fails before any gateway request', () => {
  let requests = 0;
  assert.throws(() => new CoSensClient({ apiKey: 'key', region: 'eu', fetch: async () => { requests++; return new Response('{}'); } }), /COSENS_REGION/);
  assert.throws(() => new CoSensClient({ apiKey: 'key', region: 'cn', fetch: async () => { requests++; return new Response('{}'); } }), /COSENS_REGION/);
  assert.equal(requests, 0);
});

test('client uses the shared key for the US API', async () => {
  const previous = Object.fromEntries(['COSENS_REGION', 'COSENS_API_KEY'].map(name => [name, process.env[name]]));
  try {
    process.env.COSENS_API_KEY = 'shared-key';
    const client = new CoSensClient({ region: 'us' });
    assert.equal(client.transport.apiKey, 'shared-key');
    await client.close();
    process.env.COSENS_API_KEY = 'fallback-key';
    const fallback = new CoSensClient({ region: 'us' });
    assert.equal(fallback.transport.apiKey, 'fallback-key');
    await fallback.close();
    const explicit = new CoSensClient({ apiKey: 'explicit-key', region: 'us' });
    assert.equal(explicit.transport.apiKey, 'explicit-key');
    await explicit.close();
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name]; else process.env[name] = value;
    }
  }
});
