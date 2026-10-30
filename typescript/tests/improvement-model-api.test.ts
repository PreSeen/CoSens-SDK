import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient, type BusinessModelAPI } from '../src/index.ts';

test('improvement and resume transmit an explicit model connection using the existing data envelope', async () => {
  const seen: RequestInit[] = [];
  const paths: string[] = [];
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    paths.push(new URL(String(input)).pathname);
    seen.push(init ?? {});
    return new Response(JSON.stringify({ job_id: 'j1', status: 'queued' }));
  } });
  const modelApi: BusinessModelAPI = { baseUrl: 'https://business.example/v1', model: 'business-model', apiKey: 'model-key' };
  await client.atoms.improve('r1', { reason: 'Improve', key: 'stable-key', feedback: 'Missing rows', modelApi });
  await client.atoms.resumeImprovement('j1', { modelApi });
  await client.atoms.resumeImprovement('j1');
  await client.atoms.confirmAttribution('j1', { candidateId: 'c1', candidateSha256: 'frozen-hash' });
  const wire = { base_url: modelApi.baseUrl, model: modelApi.model, api_key: modelApi.apiKey };
  assert.deepEqual(JSON.parse(String(seen[0]!.body)), { data: { reason: 'Improve', feedback: 'Missing rows', model_api: wire } });
  assert.equal(new Headers(seen[0]!.headers).get('Idempotency-Key'), 'stable-key');
  assert.deepEqual(JSON.parse(String(seen[1]!.body)), { data: { model_api: wire } });
  assert.equal(seen[2]!.body, undefined);
  assert.deepEqual(JSON.parse(String(seen[3]!.body)), { data: { candidate_id: 'c1', candidate_sha256: 'frozen-hash' } });
  assert.ok(paths[3]!.endsWith('/improvements/j1/attribution/confirm'));
  assert.ok(seen.every(r => new Headers(r.headers).get('Authorization') === 'Bearer gateway-key'));
  await client.close();
});

test('gateway failures cannot echo business model credentials', async () => {
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async () => new Response(JSON.stringify({
    error: { message: 'Rejected secret-model-key', code: 'secret-model-key' },
  }), { status: 401, headers: { 'x-request-id': 'secret-model-key' } }) });
  const modelApi = { baseUrl: 'https://business.example/v1', model: 'm', apiKey: 'secret-model-key' };
  for (const run of [() => client.atoms.improve('r1', { reason: 'Improve', modelApi }), () => client.atoms.resumeImprovement('j1', { modelApi })]) {
    await assert.rejects(run, (error: any) => {
      assert.ok(!error.message.includes('secret-model-key'));
      assert.equal(error.code, '[REDACTED]');
      assert.equal(error.requestId, '[REDACTED]');
      return true;
    });
  }
  await client.close();
});

test('invalid model connections fail before contacting the gateway', async () => {
  let sent = 0;
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async () => { sent++; return new Response('{}'); } });
  for (const extra of [{ baseUrl: 'file:///etc/passwd' }, { apiKey: 'key\r\n' }, { agent_argv: ['sh'] }]) {
    assert.throws(() => client.atoms.improve('r1', { reason: 'Improve', modelApi: { baseUrl: 'https://business.example/v1', model: 'm', ...extra } }), /modelApi/);
  }
  assert.equal(sent, 0);
  await client.close();
});
