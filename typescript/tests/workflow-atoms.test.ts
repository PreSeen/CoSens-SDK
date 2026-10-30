import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient } from '../src/index.ts';

test('task workflow atoms preserve routes, bodies, keys and gateway authentication', async () => {
  const seen: Array<{ method: string; path: string; body: unknown; key: string | null; auth: string | null; type: string | null }> = [];
  const client = new CoSensClient({ apiKey: 'example-key', fetch: async (input, init) => {
    seen.push({ method: init?.method ?? '', path: new URL(String(input)).pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined,
      key: new Headers(init?.headers).get('Idempotency-Key'), auth: new Headers(init?.headers).get('Authorization'), type: new Headers(init?.headers).get('X-Auth-Type') });
    return new Response('{}');
  } });
  const a = client.atoms;
  const cases: Array<[() => Promise<unknown>, string, string, unknown, string?]> = [
    [() => a.getArtifact('a1'), 'GET', '/api/v1/artifacts/a1', undefined],
    [() => a.createTask({ source: 'new_task', objective: 'work', artifacts: ['a1'], key: 'task-key' }), 'POST', '/api/v1/tasks', { source: 'new_task', objective: 'work', artifacts: ['a1'] }, 'task-key'],
    [() => a.getTask('t1'), 'GET', '/api/v1/tasks/t1', undefined],
    [() => a.supplementTaskContext('t1', { expectedContextVersion: 1, selfContained: true }), 'POST', '/api/v1/tasks/t1/context', { expected_context_version: 1, self_contained: true }],
    [() => a.startExternalAnalysis('t1', { key: 'analysis-key' }), 'POST', '/api/v1/tasks/t1/analysis', undefined, 'analysis-key'],
    [() => a.getExternalAnalysis('an1'), 'GET', '/api/v1/task-analyses/an1', undefined],
    [() => a.resumeExternalAnalysis('an1'), 'POST', '/api/v1/task-analyses/an1/resume', undefined],
    [() => a.startExecution('t1', { key: 'execution-key' }), 'POST', '/api/v1/tasks/t1/executions', undefined, 'execution-key'],
    [() => a.startReconstruction('t1', { key: 'reconstruction-key' }), 'POST', '/api/v1/tasks/t1/reconstructions', undefined, 'reconstruction-key'],
    [() => a.getExecution('ex1'), 'GET', '/api/v1/executions/ex1', undefined],
    [() => a.submitExecution('ex1', { requestSha256: 'hash1', outcome: 'completed', artifacts: [{ artifact_id: 'a2', role: 'submitted_output' }] }), 'POST', '/api/v1/executions/ex1/result', { request_sha256: 'hash1', outcome: 'completed', artifacts: [{ artifact_id: 'a2', role: 'submitted_output' }], events: [], trace_coverage: 'unknown' }],
    [() => a.getRevision('r1'), 'GET', '/api/v1/revisions/r1', undefined],
    [() => a.startAttribution('r1', { key: 'attribution-key' }), 'POST', '/api/v1/revisions/r1/attributions', { data: {} }, 'attribution-key'],
    [() => a.getAttribution('at1'), 'GET', '/api/v1/attributions/at1', undefined],
    [() => a.resumeAttribution('at1'), 'POST', '/api/v1/attributions/at1/resume', undefined],
    [() => a.selectCause('at1', { candidateSha256: 'hash2', customReason: 'reason' }), 'POST', '/api/v1/attributions/at1/selection', { data: { candidate_sha256: 'hash2', custom_reason: 'reason' } }],
    [() => a.startRevision('r1', { attributionId: 'at1', key: 'revision-key' }), 'POST', '/api/v1/revisions/r1/revisions', { data: { attribution_id: 'at1' } }, 'revision-key'],
    [() => a.acceptRevision('r1', { artifactHashes: { a2: 'server-hash' } }), 'POST', '/api/v1/revisions/r1/acceptance', { data: { artifact_hashes: { a2: 'server-hash' } } }],
    [() => a.startRecipeCandidate('t1', { revisionId: 'r1', key: 'recipe-key' }), 'POST', '/api/v1/tasks/t1/recipe-candidates', { data: { revision_id: 'r1', include_memory: false } }, 'recipe-key'],
    [() => a.getRecipeCandidate('rc1'), 'GET', '/api/v1/recipe-candidates/rc1', undefined],
    [() => a.resumeRecipeCandidate('rc1'), 'POST', '/api/v1/recipe-candidates/rc1/resume', undefined],
    [() => a.startVerification('rc1', { key: 'verification-key' }), 'POST', '/api/v1/recipe-candidates/rc1/verifications', undefined, 'verification-key'],
    [() => a.getVerification('v1'), 'GET', '/api/v1/recipe-verifications/v1', undefined],
  ];
  for (const [call] of cases) await call();
  assert.deepEqual(seen.map(x => [x.method, x.path, x.body, x.key]), cases.map(([, method, path, body, key]) => [method, path, body, key ?? null]));
  assert.ok(seen.every(x => x.auth === 'Bearer example-key' && x.type === 'api_key'));
  await client.close();
});

test('workflow atoms reject unsafe requests locally', async () => {
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { calls++; return new Response('{}'); } });
  assert.throws(() => client.atoms.startExecution('t1', { key: '' }), /key/);
  assert.throws(() => client.atoms.selectCause('a1', { candidateSha256: 'hash', candidateId: 'c1', customReason: 'other' }), /exactly one/);
  assert.throws(() => client.atoms.submitExecution('e1', { requestSha256: 'hash', outcome: 'completed', artifacts: [] }), /submitted_output/);
  assert.throws(() => client.atoms.supplementTaskContext('t1', { expectedContextVersion: 0 }), /positive integer/);
  assert.equal(calls, 0);
});

test('workflow job polling stops at awaiting_runner and rejects invalid timeout before network', async () => {
  let requests = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { requests++; return new Response(JSON.stringify({ status: 'awaiting_runner', analysis_id: 'an1' })); } });
  await assert.rejects(client.atoms.waitWorkflowJob('analysis', 'an1', { timeout: Number.NaN }), /timeout/);
  assert.equal(requests, 0);
  const job = await client.atoms.waitWorkflowJob('analysis', 'an1', { timeout: 0 });
  assert.equal(job.status, 'awaiting_runner');
  assert.equal(requests, 1);
});

test('workflow atoms reject conflicting artifact roles and oversized result JSON before network', async () => {
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => { calls++; return new Response('{}'); } });
  assert.throws(() => client.atoms.createTask({ source: 'new_task', objective: 'work', key: 'k1', artifacts: [
    { artifact_id: 'same', role: 'original_input' }, { artifact_id: 'same', role: 'submitted_output' },
  ] }), /both original_input and submitted_output/);
  assert.throws(() => client.atoms.submitExecution('e1', { requestSha256: 'hash', outcome: 'completed',
    artifacts: [{ artifact_id: 'a1', role: 'submitted_output' }],
    events: [{ id: 'event-1', kind: 'tool_result', payload: { large: 'x'.repeat(1_000_000) } }],
  }), /at most 1,000,000 bytes/);
  assert.equal(calls, 0);
  await client.close();
});
