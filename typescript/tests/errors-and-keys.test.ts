import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient, RSICandidateFailedError } from '../src/index.ts';
import { recipeRequestKey } from '../src/http.ts';

const DETAIL = { code: 'insufficient_credits', message: 'The model gateway reported insufficient credits.',
  retryable: false, phase: 'analyzing', upstream_status: 402, upstream_code: 'gateway_credit_insufficient' };
const json = (value: unknown, headers: Record<string, string> = {}) => new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json', ...headers } });

test('successful responses carry requestId without changing enumerable fields', async () => {
  const client = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ task_id: 'task_2' }, { 'x-request-id': 'req-123' }) });
  const task = await client.atoms.getTask('task_2');
  assert.deepEqual(task, { task_id: 'task_2' });
  assert.equal(JSON.stringify(task), '{"task_id":"task_2"}');
  assert.equal(task.requestId, 'req-123');
  const missing = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ task_id: 't' }) });
  assert.equal((await missing.atoms.getTask('t')).requestId, undefined);
});

for (const [name, payload, expected] of [
  ['error_detail', { error: 'insufficient_credits', error_detail: DETAIL },
    { code: 'insufficient_credits', retryable: false, phase: 'analyzing', upstreamStatus: 402, upstreamCode: 'gateway_credit_insufficient' }],
  ['a bare error code', { error: 'ValueError', phase: 'generating' },
    { code: 'ValueError', retryable: undefined, phase: 'generating', upstreamStatus: undefined, upstreamCode: undefined }],
] as const) {
  test(`waitWorkflowJob throws RSICandidateFailedError from ${name}`, async () => {
    const client = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ recipe_candidate_id: 'candidate_1', status: 'failed', ...payload }, { 'x-request-id': 'req-9' }) });
    const error = await client.atoms.waitWorkflowJob('recipe_candidate', 'candidate_1', { timeout: 1 }).then(() => undefined, value => value);
    assert.ok(error instanceof RSICandidateFailedError);
    assert.equal(error.candidateId, 'candidate_1');
    assert.equal(error.requestId, 'req-9');
    assert.deepEqual(Object.fromEntries(Object.keys(expected).map(key => [key, (error as any)[key]])), expected);
  });
}

test('waitWorkflowJob returns other terminal states', async () => {
  const client = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ recipe_candidate_id: 'c', status: 'interrupted' }) });
  assert.equal((await client.atoms.waitWorkflowJob('recipe_candidate', 'c', { timeout: 1 })).status, 'interrupted');
});

test('the default Recipe key is deterministic and matches the Python SDK', async () => {
  // The same vectors are asserted in python/tests/test_errors_and_keys.py.
  assert.equal(recipeRequestKey('task_1', 'revision_1'), 'recipe-f1880ef284cfb24705367206c39a433e643c3907f6e2953c9a44491e3d9d8513');
  assert.equal(recipeRequestKey('task_1', 'revision_1', 200_000), 'recipe-8f22d4789600f0311e6526ff172a97e48da326ad7af0e9899afc2fad09bf0c75');
  const keys: Array<string | null> = [];
  const client = new CoSensClient({ apiKey: 'service-key', fetch: async (_input, init) => {
    keys.push(new Headers(init?.headers).get('Idempotency-Key'));
    return json({ recipe_candidate_id: 'c', status: 'queued' });
  } });
  await client.atoms.startRecipeCandidate('task_1', { revisionId: 'revision_1' });
  await client.atoms.startRecipeCandidate('task_1', { revisionId: 'revision_1' });
  await client.atoms.startRecipeCandidate('task_1', { revisionId: 'revision_1', maxCompletionTokens: 200_000 });
  await client.atoms.startRecipeCandidate('task_1', { revisionId: 'revision_1', key: 'explicit' });
  assert.deepEqual(keys, [recipeRequestKey('task_1', 'revision_1'), recipeRequestKey('task_1', 'revision_1'),
    recipeRequestKey('task_1', 'revision_1', 200_000), 'explicit']);
});
