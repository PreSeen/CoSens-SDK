import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensClient, TurnStore } from '../src/index.ts';

test('CN and US clients retain their configured gateway hosts', async () => {
  const hosts: string[] = [];
  const fetcher: typeof fetch = async input => {
    hosts.push(new URL(String(input)).host);
    return new Response(JSON.stringify({ run_id: 'r1', status: 'ready' }));
  };
  const cn = new CoSensClient({ apiKey: 'key', region: 'cn', fetch: fetcher });
  const us = new CoSensClient({ apiKey: 'key', region: 'us', fetch: fetcher });
  await cn.atoms.getRun('r1');
  await us.atoms.getRun('r1');
  assert.deepEqual(hosts, ['friday-rsi-cn.stardust-ai.com', 'rsi.cosens.ai']);
  await cn.close(); await us.close();
});

test('turn state is isolated by service identity even for the same API Key', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'cosens-region-')), 'state.sqlite3');
  const cn = new TurnStore(path, 'key', 'friday-rsi-cn.stardust-ai.com/api');
  const us = new TurnStore(path, 'key', 'rsi.cosens.ai/api');
  cn.put('turn-1', { state: 'completed' });
  assert.equal(us.get('turn-1'), undefined);
  us.put('turn-1', { state: 'pre_execution_pending' });
  assert.equal(cn.get('turn-1')?.state, 'completed');
  cn.close(); us.close();
});

test('invalid region fails before any gateway request', () => {
  let requests = 0;
  assert.throws(() => new CoSensClient({ apiKey: 'key', region: 'eu', fetch: async () => { requests++; return new Response('{}'); } }), /COSENS_REGION/);
  assert.equal(requests, 0);
});

test('client selects the regional gateway key before the generic key', async () => {
  const previous = Object.fromEntries(['COSENS_REGION', 'RSI_API_KEY_CN', 'RSI_API_KEY_US', 'RSI_API_KEY'].map(name => [name, process.env[name]]));
  try {
    process.env.RSI_API_KEY_CN = 'cn-key';
    process.env.RSI_API_KEY_US = 'us-key';
    process.env.RSI_API_KEY = 'fallback-key';
    for (const [region, expected] of [['cn', 'cn-key'], ['us', 'us-key']] as const) {
      const client = new CoSensClient({ region });
      assert.equal(client.transport.apiKey, expected);
      await client.close();
    }
    process.env.RSI_API_KEY_US = '';
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
