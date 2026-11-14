import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient, RSILearningFailedError, RSILearningStoppedError } from '../src/index.ts';

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
  ['a bare error code', { error: 'ValueError', error_detail: null, phase: 'generating' },
    { code: 'ValueError', retryable: undefined, phase: 'generating', upstreamStatus: undefined, upstreamCode: undefined }],
] as const) {
  test(`waitLearning throws RSILearningFailedError from ${name}`, async () => {
    const client = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ execution_id: 'execution_1', learning: { execution_id: 'execution_1', status: 'failed', ...payload } }, { 'x-request-id': 'req-9' }) });
    const error = await client.atoms.waitLearning('execution_1', { timeout: 1 }).then(() => undefined, value => value);
    assert.ok(error instanceof RSILearningFailedError);
    assert.equal(error.executionId, 'execution_1');
    assert.equal(error.requestId, 'req-9');
    assert.deepEqual(Object.fromEntries(Object.keys(expected).map(key => [key, (error as any)[key]])), expected);
  });
}

test('waitLearning stops on interrupted with the learning', async () => {
  const learning = { execution_id: 'e', status: 'interrupted' };
  const client = new CoSensClient({ apiKey: 'service-key', fetch: async () => json({ execution_id: 'e', learning }) });
  const error = await client.atoms.waitLearning('e', { timeout: 1 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSILearningStoppedError);
  assert.equal(error.status, 'interrupted'); assert.deepEqual(error.learning, learning);
});
