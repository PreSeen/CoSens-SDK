import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Agent } from '@openai/agents';
import { CoSensClient, CoSensAgentRunner, cosens, RSIHTTPError, RSILearningStoppedError } from '../src/index.ts';
import { ServiceFake } from './service-fake.ts';

const statePath = () => join(mkdtempSync(join(tmpdir(), 'explicit-ts-')), 'state.sqlite');
const nativeClient = () => ({
  responses: { create: async () => ({ id: 'response_1', status: 'completed', output_text: 'final output', output: [{ type: 'message' }] }) },
  chat: { completions: { create: async () => ({ choices: [{ message: { content: JSON.stringify({ activate: true, reason: 'task' }) } }] }) } },
});
const agentRunner = (fetcher: typeof fetch, path: string) => {
  const agent = new Agent({ name: 'reporter', instructions: 'Produce output', model: 'model' });
  return { agent, wrapper: new CoSensAgentRunner(agent, { apiKey: 'test-key', mode: 'disable', fetch: fetcher, statePath: path,
    runner: { run: async () => ({ finalOutput: 'final output', newItems: [], interruptions: [] }) } }) };
};
const noRevisionOrCandidate = (value: object) => assert.deepEqual(Object.keys(value).filter(key => /revision|candidate/i.test(key)), []);

for (const entry of ['transparent', 'agent', 'atomic']) test(`explicit learning and a correction: ${entry}`, async () => {
  const service = new ServiceFake();
  const owner = 'ts-http-' + randomUUID(); const local = process.env.COSENS_TEST_ALGORITHM_HTTP_URL;
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input)); const path = url.pathname.replace(/^\/api/, '');
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
    if (path.endsWith('/executions') && !body?.previous_execution_id) assert.equal(body.model, 'model');
    if (local) { const headers = new Headers(init?.headers); headers.set('X-User-Id', owner); return fetch(local + path + url.search, { ...init, headers }); }
    return service.fetch(input, init);
  };
  const path = statePath();
  const client = new CoSensClient({ apiKey: 'test-key', fetch: fetcher, statePath: path });
  let wrapper: any; let taskId: string; let executionId: string;
  if (entry === 'transparent') {
    wrapper = cosens(nativeClient(), { apiKey: 'test-key', mode: 'auto', fetch: fetcher, statePath: path });
    assert.equal((await wrapper.responses.create({ model: 'model', input: 'Produce output' })).output_text, 'final output');
    const turn = wrapper.cosens.getTurn(wrapper.cosens.lastTurnId); assert.equal(turn.state, 'completed'); noRevisionOrCandidate(turn);
    taskId = turn.taskId; executionId = turn.executionId;
  } else if (entry === 'agent') {
    const runner = agentRunner(fetcher, path); wrapper = runner.wrapper;
    assert.equal((await wrapper.run(runner.agent, 'Produce output')).finalOutput, 'final output');
    const turn = wrapper.cosens.getTurn(wrapper.cosens.lastTurnId); assert.equal(turn.state, 'completed'); noRevisionOrCandidate(turn);
    taskId = turn.taskId; executionId = turn.executionId;
  } else {
    const task = await client.atoms.createTask({ source: 'new_task', objective: 'Produce output', selfContained: true, key: 'task' }); taskId = task.task_id;
    const execution = await client.atoms.startExecution(taskId, { key: 'execution', model: 'model' });
    const artifact = await client.atoms.artifact({ name: 'output.txt', content: 'final output' });
    const result = await client.atoms.submitExecution(execution.execution_id, { requestSha256: execution.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: artifact.artifact_id, role: 'submitted_output' }] });
    assert.equal(result.status, 'completed'); noRevisionOrCandidate(result); executionId = result.execution_id;
  }
  assert.ok(taskId! && executionId!);
  if (!local) assert.equal(service.generations, 0);
  if (entry === 'transparent') await wrapper.cosens.generateRecipe();
  else if (entry === 'agent') await wrapper.cosens.generateRecipe();
  else await client.atoms.learn(executionId!);
  const v1 = await client.atoms.waitLearning(executionId!, { timeout: 10 });
  assert.equal(v1.status, 'completed'); assert.equal(v1.version, 1); assert.equal(v1.previous_version, null);
  await client.atoms.recordFeedback(executionId!, { feedback: 'Use JSON without code fences', source: 'human', key: 'feedback' });
  const correction = await client.atoms.startExecution(taskId!, { key: 'correction', previousExecutionId: executionId! });
  assert.equal(correction.kind, 'correction'); assert.notEqual(correction.execution_id, executionId!);
  const artifact = await client.atoms.artifact({ name: 'corrected.json', content: '{"answer":42}' });
  const final = await client.atoms.submitExecution(correction.execution_id, { requestSha256: correction.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: artifact.artifact_id, role: 'submitted_output' }] });
  assert.equal(final.trace_origin, 'correction');
  await client.atoms.learn(correction.execution_id);
  const v2 = await client.atoms.waitLearning(correction.execution_id, { timeout: 10 });
  assert.equal(v2.status, 'completed'); assert.equal(v2.version, 2); assert.equal(v2.previous_version, 1);
  assert.equal(v2.recipe_id, v1.recipe_id); assert.ok(v2.attribution.possible_causes.length);
  await wrapper?.close(); await client.close();
});

async function completedExecution(client: CoSensClient) {
  const prepared = await client.start('Produce output', { requestKey: 'prepare-1' });
  const artifact = await client.atoms.artifact({ name: 'output.txt', content: 'final' });
  await client.atoms.submitExecution(prepared.execution_id, { requestSha256: prepared.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: artifact.artifact_id, role: 'submitted_output' }] });
  return prepared;
}

test('start phases correct and learn', async () => {
  const service = new ServiceFake();
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  const prepared = await completedExecution(client);
  const snapshot = { task_id: prepared.task_id, execution_id: prepared.execution_id };
  const corrected = await client.start(snapshot, { phase: 'correct', requestKey: 'fix-1' });
  assert.equal(corrected.kind, 'correction'); assert.notEqual(corrected.execution_id, prepared.execution_id);
  const last = service.requests.at(-1)!;
  assert.deepEqual([last.method, last.path, last.body], ['POST', '/tasks/task_1/executions', { previous_execution_id: prepared.execution_id }]);
  assert.equal(last.headers.get('idempotency-key'), 'fix-1');
  const learning = await client.start(snapshot, { phase: 'learn', model: 'public-model' });
  assert.equal(learning.execution_id, prepared.execution_id); assert.equal(learning.status, 'queued');
  assert.deepEqual([service.requests.at(-1)!.path, service.requests.at(-1)!.body], [`/executions/${prepared.execution_id}/learn`, { model: 'public-model' }]);
  await assert.rejects(client.start(snapshot, { phase: 'recipe' as any, requestKey: 'k' }), /prepare, correct or learn/);
  await assert.rejects(client.start(snapshot, { phase: 'correct' }), /key/);
});

for (const extra of [{ recipeId: 'recipe_1' }, { recallMemory: true }]) {
  test(`start correct rejects ${Object.keys(extra)[0]} before sending`, async () => {
    const service = new ServiceFake();
    const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
    await assert.rejects(client.start({ task_id: 'task_1', execution_id: 'execution_1' }, { phase: 'correct', requestKey: 'fix-1', ...extra }), /previousExecutionId/);
    assert.deepEqual(service.requests, []);
  });
}

test('feedback on a turn, then a correction of its execution', async () => {
  const service = new ServiceFake(); const path = statePath();
  const wrapper = cosens(nativeClient(), { apiKey: 'test-key', mode: 'auto', fetch: service.fetch, statePath: path });
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  await wrapper.responses.create({ model: 'model', input: 'Produce output' });
  const turn = wrapper.cosens.getTurn(wrapper.cosens.lastTurnId)!;
  await wrapper.cosens.feedback('The totals are wrong', { source: 'human' });
  const feedback = service.requests.at(-1)!;
  assert.deepEqual([feedback.path, feedback.body], [`/executions/${turn.executionId}/feedback`, { feedback: 'The totals are wrong', source: 'human' }]);
  const corrected = await client.start({ task_id: turn.taskId!, execution_id: turn.executionId! }, { phase: 'correct', requestKey: 'correct:' + turn.turnId });
  assert.equal(corrected.kind, 'correction'); assert.notEqual(corrected.execution_id, turn.executionId);
  await wrapper.close();
});

test('the agent wrapper surfaces a changed learning budget', async () => {
  const service = new ServiceFake(); const { agent, wrapper } = agentRunner(service.fetch, statePath());
  await wrapper.run(agent, 'Produce output');
  await wrapper.cosens.generateRecipe(undefined, { maxCompletionTokens: 200_000 });
  assert.equal((await wrapper.cosens.generateRecipe()).max_completion_tokens, 200_000);
  const error: any = await wrapper.cosens.generateRecipe(undefined, { maxCompletionTokens: 300_000 }).then(() => undefined, (value: unknown) => value);
  assert.ok(error instanceof RSIHTTPError); assert.equal(error.statusCode, 422); assert.equal(error.code, 'invalid_request');
  assert.equal(service.generations, 1);
  assert.equal(wrapper.cosens.getTurn(wrapper.cosens.lastTurnId!)?.learning?.max_completion_tokens, 200_000);
  await wrapper.close();
});

test('feedback and resume follow the service rules', async () => {
  const service = new ServiceFake();
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  const pending = await client.start('Produce output', { requestKey: 'prepare-1' });
  await assert.rejects(client.atoms.recordFeedback(pending.execution_id, { feedback: 'f', source: 'human', key: 'early' }), (error: any) => error.statusCode === 422);
  const artifact = await client.atoms.artifact({ name: 'output.txt', content: 'final' });
  await client.atoms.submitExecution(pending.execution_id, { requestSha256: pending.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: artifact.artifact_id, role: 'submitted_output' }] });
  await assert.rejects(client.atoms.resumeLearning(pending.execution_id), /has not learned yet/);
  const learning = await client.atoms.learn(pending.execution_id, { maxCompletionTokens: 200_000 });
  assert.deepEqual(await client.atoms.resumeLearning(pending.execution_id), learning);
  await assert.rejects(client.atoms.resumeLearning(pending.execution_id, { maxCompletionTokens: 300_000 }), /completion budget/);
});

test('a stopped learning surfaces to the caller and stays resumable', async () => {
  const service = new ServiceFake(); const path = statePath();
  const wrapper = cosens(nativeClient(), { apiKey: 'test-key', mode: 'auto', fetch: service.fetch, statePath: path });
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  await wrapper.responses.create({ model: 'model', input: 'Produce output' });
  const learning = await wrapper.cosens.generateRecipe();
  assert.equal(wrapper.cosens.getTurn(wrapper.cosens.lastTurnId)?.learning?.execution_id, learning.execution_id);
  service.learnings.set(learning.execution_id, { ...service.learnings.get(learning.execution_id)!, status: 'interrupted' });
  const error: any = await client.atoms.waitLearning(learning.execution_id, { timeout: 1 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSILearningStoppedError); assert.equal(error.status, 'interrupted');
  assert.equal((await client.atoms.resumeLearning(learning.execution_id)).status, 'queued');
  assert.equal((await client.atoms.waitLearning(learning.execution_id, { timeout: 1 })).status, 'completed');
  await wrapper.close();
});
