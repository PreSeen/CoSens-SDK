/** Validate the TypeScript atomic Run path against a real gateway with synthetic text. */
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { CoSensClient } from '../src/index.ts';
import { fridayKey } from './common.ts';

if (!process.argv.includes('--write')) throw new Error('Pass --write to create a synthetic Artifact and Run');
const outputIndex = process.argv.indexOf('--output');
const outputPath = outputIndex >= 0 ? process.argv[outputIndex + 1] : undefined;
if (outputIndex >= 0 && !outputPath) throw new Error('--output requires a file path');
const apiKey = fridayKey();
const tag = `sdk-ts-atomic-${randomUUID()}`;
const requests: Array<{ method: string; path: string; status: number; authType: string; authorization: string; body?: unknown; requestId: string | null }> = [];
const checkedFetch: typeof fetch = async (input, init) => {
  const headers = new Headers(init?.headers);
  assert.equal(headers.get('x-auth-type'), 'api_key');
  assert.equal(headers.get('authorization'), `Bearer ${apiKey}`);
  assert.equal(headers.has('x-user-id') || headers.has('x_user_id'), false);
  const url = new URL(String(input));
  assert.equal(url.protocol, 'https:');
  assert.ok(url.pathname.startsWith('/api/'));
  const response = await fetch(input, init);
  requests.push({ method: init?.method ?? 'GET', path: url.pathname + url.search, status: response.status,
    authType: headers.get('x-auth-type')!, authorization: 'Bearer <redacted>',
    ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}), requestId: response.headers.get('x-request-id') });
  return response;
};

const client = new CoSensClient({ apiKey, fetch: checkedFetch });
try {
  const source = await client.atoms.artifact({ name: `${tag}-input.txt`, content: `${tag}: synthetic TypeScript SDK atomic validation` });
  assert.equal(typeof source.artifact_id, 'string');
  assert.equal(typeof source.content_hash, 'string');
  const options = { objective: `${tag}: synthetic atomic Run`, taskFamily: 'general', inputArtifactIds: [source.artifact_id], key: `${tag}:prepare` };
  const prepared = await client.atoms.prepareReady(options);
  const replay = await client.atoms.prepare(options);
  assert.equal(replay.run_id, prepared.run_id);
  const run = await client.atoms.getRun(prepared.run_id);
  assert.equal(run.run_id, prepared.run_id);
  assert.equal(run.status, 'ready');
  assert.ok(run.snapshot && typeof run.snapshot === 'object');
  assert.ok(prepared.recipe && typeof prepared.recipe === 'object');
  assert.ok(prepared.context && typeof prepared.context === 'object');
  const events = [
    { id: `${tag}:user`, kind: 'user_prompt', payload: { role: 'user', content: options.objective, source_revision: 'local_synthetic_fixture' } },
    { id: `${tag}:assistant`, kind: 'agent_action', payload: { role: 'assistant', content: 'local synthetic result', source_revision: 'local_synthetic_fixture' } },
  ];
  for (let attempt = 0; attempt < 2; attempt++) {
    const ack = await client.atoms.sessionEvents(run.run_id, events);
    assert.ok(Array.isArray(ack.durable_ack));
    assert.ok(events.every(event => ack.durable_ack.includes(event.id)));
  }
  const receipt = await client.atoms.submit(run.run_id, { data: { summary: 'local synthetic result', outcome: 'completed' } });
  assert.equal(receipt.status, 'completed');
  const trace = await client.atoms.getTrace(run.run_id);
  const ids = (trace.session_events ?? []).map((event: any) => event.id);
  assert.ok(events.every(event => ids.filter((id: string) => id === event.id).length === 1));
  const report = { tag, region: client.transport.service.region, runId: run.run_id, artifactId: source.artifact_id,
    prepareIdempotency: 'passed', durableAckReplay: 'passed', traceDeduplication: 'passed', requests };
  if (outputPath) {
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify({ tag, runId: run.run_id, status: receipt.status, requestCount: requests.length, outputPath: outputPath ?? null }));
} finally {
  await client.close();
}
