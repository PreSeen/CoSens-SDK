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
    [() => a.getTaskRecipe('t1'), 'GET', '/api/v1/tasks/t1/recipe', undefined],
    [() => a.getRecipeVersions('rc1'), 'GET', '/api/v1/recipe-chains/rc1/versions', undefined],
    [() => a.improveTask('t1', { revisionId: 'r1', feedback: 'Review units', key: 'improve-key' }), 'POST', '/api/v1/tasks/t1/improvements', { revision_id: 'r1', feedback: 'Review units' }, 'improve-key'],
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

test('Task execution and improvement transmit caller model settings in the root body', async () => {
  const seen: Array<{ path: string; body: any; key: string | null; auth: string | null }> = [];
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async (input, init) => {
    const headers = new Headers(init?.headers);
    seen.push({ path: new URL(String(input)).pathname, body: init?.body ? JSON.parse(String(init.body)) : undefined,
      key: headers.get('Idempotency-Key'), auth: headers.get('Authorization') });
    return new Response('{}');
  } });
  const modelApi = { baseUrl: 'https://business.example/v1', model: 'business-model', apiKey: 'model-secret', temperature: 0.2, maxTokens: 4096 };
  const wire = { base_url: modelApi.baseUrl, model: modelApi.model, api_key: modelApi.apiKey, temperature: 0.2, max_tokens: 4096 };
  await client.atoms.startExecution('t/1', { key: 'execution', modelApi, recipeId: 'recipe-1', includeMemory: true });
  await client.atoms.improveTask('t/1', { key: 'improvement', revisionId: 'rev-1', feedback: 'Keep signs', modelApi });
  await client.atoms.startExecution('t/1', { key: 'recipe-only', recipeId: 'recipe-1' });
  await client.atoms.startExecution('t/1', { key: 'memory-only', includeMemory: true });
  await client.atoms.startExecution('t/1', { key: 'legacy', includeMemory: false });
  assert.deepEqual(seen.map(x => x.body), [
    { model_api: wire, recipe_id: 'recipe-1', include_memory: true },
    { revision_id: 'rev-1', feedback: 'Keep signs', model_api: wire },
    { recipe_id: 'recipe-1' }, { include_memory: true }, undefined,
  ]);
  assert.equal(seen[0]!.path, '/api/v1/tasks/t%2F1/executions');
  assert.equal(seen[1]!.path, '/api/v1/tasks/t%2F1/improvements');
  assert.deepEqual(seen.map(x => x.key), ['execution', 'improvement', 'recipe-only', 'memory-only', 'legacy']);
  assert.ok(seen.every(x => x.auth === 'Bearer gateway-key'));
  await client.close();
});

test('Task model API errors redact provider credentials and invalid inputs make no request', async () => {
  let calls = 0;
  const modelApi = { baseUrl: 'https://business.example/v1', model: 'model', apiKey: 'provider-secret' };
  const client = new CoSensClient({ apiKey: 'gateway-key', fetch: async () => {
    calls++;
    return new Response(JSON.stringify({ error: { message: 'Rejected provider-secret', code: 'provider-secret' } }),
      { status: 401, headers: { 'x-request-id': 'provider-secret' } });
  } });
  const a = client.atoms;
  for (const invalid of [{ baseUrl: 'file:///etc/passwd' }, { apiKey: 'key\r\n' }, { agent_argv: ['sh'] }]) {
    const bad = { ...modelApi, ...invalid };
    assert.throws(() => a.startExecution('t1', { key: 'k1', modelApi: bad }), /modelApi/);
    assert.throws(() => a.improveTask('t1', { key: 'k2', revisionId: 'r1', feedback: 'Review', modelApi: bad }), /modelApi/);
  }
  assert.throws(() => a.startExecution('t1', { key: 'k', recipeId: '' }), /resource ID/);
  assert.throws(() => a.startExecution('t1', { key: 'k', includeMemory: 'true' as unknown as boolean }), /includeMemory/);
  assert.throws(() => a.improveTask('t1', { key: 'k', revisionId: '', feedback: 'Review' }), /resource ID/);
  for (const feedback of ['', ' ', 'x'.repeat(4001)]) {
    assert.throws(() => a.improveTask('t1', { key: 'k', revisionId: 'r1', feedback }), /feedback/);
  }
  assert.equal(calls, 0);
  for (const call of [() => a.startExecution('t1', { key: 'k1', modelApi }),
    () => a.improveTask('t1', { key: 'k2', revisionId: 'r1', feedback: 'Review', modelApi })]) {
    await assert.rejects(call, (error: any) => {
      assert.ok(!error.message.includes('provider-secret'));
      assert.equal(error.code, '[REDACTED]');
      assert.equal(error.requestId, '[REDACTED]');
      return true;
    });
  }
  assert.equal(calls, 2);
  await client.close();
});

test('a custom cause does not require a generated candidate portfolio', async () => {
  let body: unknown;
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async (_input, init) => {
    calls++;
    body = JSON.parse(String(init?.body));
    return new Response('{}');
  } });
  await client.atoms.selectCause('at1', { customReason: 'Simulated operator hypothesis' });
  assert.deepEqual(body, { data: { custom_reason: 'Simulated operator hypothesis' } });
  assert.throws(() => client.atoms.selectCause('at1', { candidateId: 'candidate-1' }), /candidateSha256/);
  assert.equal(calls, 1);
  await client.close();
});

test('Recipe polling waits for a parent, stops at a material gap and bounds the wait', async () => {
  const states = ['awaiting_parent', 'candidate', 'awaiting_material', 'awaiting_parent'];
  let calls = 0;
  const client = new CoSensClient({ apiKey: 'key', fetch: async () => {
    return new Response(JSON.stringify({ status: states[calls++] }));
  } });
  assert.equal((await client.atoms.waitWorkflowJob('recipe_candidate', 'r1', { timeout: 5 })).status, 'candidate');
  assert.equal((await client.atoms.waitWorkflowJob('recipe_candidate', 'r2', { timeout: 0 })).status, 'awaiting_material');
  await assert.rejects(client.atoms.waitWorkflowJob('recipe_candidate', 'r3', { timeout: 0 }), /still awaiting_parent/);
  assert.equal(calls, 4);
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
