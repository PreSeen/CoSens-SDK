import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Agent } from '@openai/agents';
import { CoSensClient, CoSensAgentRunner, cosens, RSIHTTPError, RSILearningStoppedError } from '../src/index.ts';
import { DEFAULT_BUDGET, ServiceFake } from './service-fake.ts';

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
const NEW = { source: 'new_task', objective: 'Produce output', selfContained: true } as const;
const policyClient = (match: string) => ({
  responses: { create: async () => ({ id: 'response_1', status: 'completed', output_text: 'final output', output: [{ type: 'message' }] }) },
  chat: { completions: { create: async (body: any) => ({ choices: [{ message: { content: JSON.stringify(
    body.response_format.json_schema.name === 'cosens_recipe_match' ? { recipe_id: match } : { activate: true, reason: 'task' }) } }] }) } },
});
const recipeView = (recipeId: string, taskId: string, version = 1) => ({ recipe_id: recipeId, origin: 'generated', version, task_id: taskId,
  recipe: { title: 'Quarterly report', when: 'Writing a report', when_not: 'Chat', lessons: [], steps: [{ id: 's1', instruction: 'Use the Q3 numbers' }] } });

for (const entry of ['transparent', 'agent', 'atomic']) test(`explicit learning and a correction: ${entry}`, async () => {
  const service = new ServiceFake();
  const owner = 'ts-http-' + randomUUID(); const local = process.env.COSENS_TEST_ALGORITHM_HTTP_URL;
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input)); const path = url.pathname.replace(/^\/api/, '');
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
    if (path === '/executions' && !body?.previous_execution_id) assert.equal(body.model, 'model');
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
    const task = await client.atoms.createTask({ objective: 'Produce output', key: 'task' }); taskId = task.task_id;
    assert.deepEqual(task, { task_id: 'task_1', objective: 'Produce output', artifacts: [], context_version: 1 });
    const execution = await client.atoms.startExecution(taskId, { key: 'execution', model: 'model', ...NEW });
    assert.equal(execution.instance_id, execution.execution_id);
    assert.deepEqual(execution.request.input, { objective: 'Produce output', artifacts: [], self_contained: true });
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
  assert.equal(correction.instance_id, executionId!);
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
  assert.deepEqual([last.method, last.path, last.body], ['POST', '/executions', { task_id: 'task_1', previous_execution_id: prepared.execution_id }]);
  assert.equal(last.headers.get('idempotency-key'), 'fix-1');
  const learning = await client.start(snapshot, { phase: 'learn', model: 'public-model' });
  assert.equal(learning.execution_id, prepared.execution_id); assert.equal(learning.status, 'queued');
  assert.deepEqual([service.requests.at(-1)!.path, service.requests.at(-1)!.body], [`/executions/${prepared.execution_id}/learn`, { model: 'public-model' }]);
  await assert.rejects(client.start(snapshot, { phase: 'recipe' as any, requestKey: 'k' }), /prepare, correct or learn/);
  await assert.rejects(client.start(snapshot, { phase: 'correct' }), /key/);
});

for (const mode of ['auto', 'approval'] as const) for (const extra of [{ recipeId: 'recipe_1' }, { recallMemory: true }]) {
  test(`start correct rejects ${Object.keys(extra)[0]} before asking or sending (${mode})`, async () => {
    const service = new ServiceFake(); const asked: unknown[] = [];
    const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
    await assert.rejects(client.start({ task_id: 'task_1', execution_id: 'execution_1' }, { phase: 'correct', requestKey: 'fix-1', mode,
      authorize: (...args) => { asked.push(args); return true; }, ...extra }), /a correction takes only previousExecutionId, taskId and model/);
    assert.deepEqual(service.requests, []); assert.deepEqual(asked, []);
  });
}

for (const snapshot of [{ task_id: 'task_1' }, { task_id: 'task_1', execution_id: '' }, { task_id: '', execution_id: 'execution_1' }, { task_id: '..', execution_id: 'execution_1' }]) {
  test(`start correct checks the snapshot ids before asking or sending: ${JSON.stringify(snapshot)}`, async () => {
    const service = new ServiceFake(); const asked: unknown[] = [];
    const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
    await assert.rejects(client.start(snapshot, { phase: 'correct', requestKey: 'fix-1', mode: 'approval', authorize: (...args) => { asked.push(args); return true; } }),
      /resource ID must be a nonempty safe path segment/);
    assert.deepEqual(service.requests, []); assert.deepEqual(asked, []);
  });
}

for (const taskId of ['', '..', 123]) {
  test(`start prepare checks the task id before asking or sending: ${JSON.stringify(taskId)}`, async () => {
    const service = new ServiceFake(); const asked: unknown[] = [];
    const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
    await assert.rejects(client.start({ task: { source: 'new_task', objective: 'Produce output' }, task_id: taskId as string },
      { requestKey: 'prepare-1', mode: 'approval', authorize: (...args) => { asked.push(args); return true; } }), /resource ID must be a nonempty safe path segment/);
    assert.deepEqual(service.requests, []); assert.deepEqual(asked, []); assert.equal(service.tasks.size, 0);
  });
}

for (const [spec, message] of [
  [{ objective: 'Produce output' }, /task.source is required/],
  [{ source: 'external_artifact', objective: 'Produce output' }, /external_artifact requires submitted_output/],
  [{ source: 'new_task', objective: 'Produce output', self_contained: 'yes' }, /selfContained must be a boolean/],
  [{ source: 'new_task' }, /objective must be nonempty/],
] as const) {
  test(`start prepare validates the spec before asking or creating a task: ${message.source}`, async () => {
    const service = new ServiceFake(); const asked: unknown[] = [];
    const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
    await assert.rejects(client.start({ task: spec as any }, { requestKey: 'prepare-1', mode: 'approval', authorize: (...args) => { asked.push(args); return true; } }), message);
    assert.deepEqual(service.requests, []); assert.deepEqual(asked, []); assert.equal(service.tasks.size, 0);
  });
}

test('start runs a named Recipe in the task that owns it', async () => {
  const service = new ServiceFake();
  const owner = service.addTask('Write the quarterly report');
  service.recipes = [recipeView('recipe_1', owner)];
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  const prepared = await client.start('Produce a report', { requestKey: 'prepare-1', recipeId: 'recipe_1' });
  assert.equal(prepared.task_id, owner); assert.equal(prepared.execution_request.method.recipe_id, 'recipe_1');
  assert.equal(prepared.execution_request.task_context.objective, 'Write the quarterly report');
  assert.deepEqual(service.requests.map(r => [r.method, r.path]), [['GET', '/recipes/recipe_1'], ['GET', `/tasks/${owner}`], ['POST', '/executions']]);
  assert.equal(service.requests.at(-1)!.body.task_id, owner); assert.equal(service.requests.at(-1)!.body.recipe_id, 'recipe_1');
  const other = service.addTask('Write the monthly report');
  const before = service.requests.length;
  await assert.rejects(client.start({ task: { source: 'new_task', objective: 'Produce a report' }, task_id: other }, { requestKey: 'prepare-2', recipeId: 'recipe_1' }),
    new RegExp(`Recipe recipe_1 belongs to task ${owner}, not ${other}`));
  assert.deepEqual(service.requests.slice(before).map(r => [r.method, r.path]), [['GET', '/recipes/recipe_1']]);
  service.recipes.push({ recipe_id: 'team', origin: 'uploaded', version: 1, recipe: { title: 'Team' } });
  await assert.rejects(client.start('Produce a report', { requestKey: 'prepare-3', recipeId: 'team' }), /Recipe team belongs to no task/);
  assert.equal(service.tasks.size, 2); assert.ok(!service.paths.includes('/tasks'));
});

test('start approval names the Recipe task and refuses a foreign one before asking', async () => {
  const service = new ServiceFake(); const asked: unknown[] = [];
  const owner = service.addTask('Write the quarterly report');
  const other = service.addTask('Write the monthly report');
  service.recipes = [recipeView('recipe_1', owner)];
  const spec = { source: 'new_task', objective: 'Produce a report' } as const;
  const authorize = (...args: unknown[]) => { asked.push(args); return true; };
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  await assert.rejects(client.start({ task: spec, task_id: other }, { requestKey: 'prepare-1', recipeId: 'recipe_1', mode: 'approval', authorize }),
    new RegExp(`Recipe recipe_1 belongs to task ${owner}, not ${other}`));
  assert.deepEqual(asked, []); assert.deepEqual(service.requests.map(r => [r.method, r.path]), [['GET', '/recipes/recipe_1']]);
  const prepared = await client.start({ task: spec }, { requestKey: 'prepare-2', recipeId: 'recipe_1', mode: 'approval', authorize });
  // The host approves the snapshot with the task the execution runs in.
  assert.deepEqual(asked, [['prepare', { task: spec, task_id: owner }]]); assert.equal(prepared.task_id, owner);
});

test('start takes a model only for a task it creates, before asking or sending', async () => {
  // An existing task keeps its frozen model, so a model with recipeId or task_id is refused before the host approves.
  const service = new ServiceFake(); const asked: unknown[] = [];
  const owner = service.addTask('Write the quarterly report');
  service.recipes = [recipeView('recipe_1', owner)];
  const client = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch });
  await client.atoms.startExecution(owner, { key: 'first', model: 'm-old', ...NEW });
  const before = service.requests.length;
  const spec = { source: 'new_task', objective: 'Produce a report' } as const;
  const authorize = (...args: unknown[]) => { asked.push(args); return true; };
  for (const [snapshot, extra] of [[{ task: spec }, { recipeId: 'recipe_1' }], [{ task: spec, task_id: owner }, {}]] as const) {
    await assert.rejects(client.start(snapshot, { requestKey: 'prepare-1', model: 'm-new', mode: 'approval', authorize, ...extra }),
      /model names the model of a task the prepare creates; an existing task keeps its own/);
  }
  assert.deepEqual(asked, []); assert.equal(service.requests.length, before);
  const prepared = await client.start({ task: spec }, { requestKey: 'prepare-2', recipeId: 'recipe_1', mode: 'approval', authorize });
  assert.equal(prepared.task_id, owner); assert.ok(!('model' in service.requests.at(-1)!.body));
});

test('auto runs a matched Recipe in the task that owns it', async () => {
  // Reusing a Recipe creates no task: the execution goes to the Recipe's task and keeps that task's model.
  const service = new ServiceFake();
  const owner = service.addTask('Write the quarterly report');
  service.recipes = [recipeView('recipe_1', owner)];
  const wrapper = cosens(policyClient('recipe_1'), { apiKey: 'test-key', mode: 'auto', fetch: service.fetch, statePath: statePath() });
  try {
    await wrapper.responses.create({ model: 'model', input: 'Produce a report' });
    assert.ok(!service.paths.includes('/tasks')); assert.deepEqual([...service.tasks.keys()], [owner]);
    const execution = service.requests.find(r => r.path === '/executions')!.body;
    assert.deepEqual([execution.task_id, execution.recipe_id, 'model' in execution], [owner, 'recipe_1', false]);
    assert.deepEqual(execution.input, { objective: 'Produce a report', artifacts: ['artifact_1'] });
    const turn = wrapper.cosens.getTurn(wrapper.cosens.lastTurnId)!;
    assert.deepEqual([turn.state, turn.taskId, turn.frozenContext.recipe.recipe_id], ['completed', owner, 'recipe_1']);
    // A turn that matches nothing makes its own task and names the model.
    service.recipes = [];
    await wrapper.responses.create({ model: 'model', input: 'Produce another report' });
    assert.deepEqual(service.requests.filter(r => r.path === '/tasks').map(r => r.body), [{ objective: 'Produce another report', artifacts: [] }]);
    assert.equal(service.requests.filter(r => r.path === '/executions').at(-1)!.body.model, 'model');
  } finally { await wrapper.close(); }
});

test('the fake keeps Recipes in their task and says what is missing', async () => {
  const service = new ServiceFake();
  const a = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch }).atoms;
  const weekly = await a.createTask({ objective: 'Weekly report', key: 'weekly' });
  assert.deepEqual(await a.createTask({ objective: 'Weekly report', key: 'weekly' }), weekly);
  await assert.rejects(a.createTask({ objective: 'Monthly report', key: 'weekly' }), (e: any) => e.statusCode === 422 && /different task/.test(e.message));
  const monthly = await a.createTask({ objective: 'Monthly report', key: 'monthly' });
  assert.notEqual(monthly.task_id, weekly.task_id); assert.equal(service.tasks.size, 2);
  service.recipes = [recipeView('weekly_recipe', weekly.task_id), recipeView('monthly_recipe', monthly.task_id)];
  await assert.rejects(a.startExecution(weekly.task_id, { key: 'e1', recipeId: 'monthly_recipe', ...NEW }), (e: any) => e.statusCode === 404);
  const started = await a.startExecution(weekly.task_id, { key: 'e1', ...NEW });
  assert.equal(started.request.method.recipe_id, 'weekly_recipe');
  assert.deepEqual(started.request.task_context, { objective: 'Weekly report', artifacts: [], context_version: 1 });
  service.recipes.push(recipeView('weekly_short', weekly.task_id));
  const choice: any = await a.startExecution(weekly.task_id, { key: 'e2', ...NEW }).then(() => undefined, value => value);
  assert.ok(choice instanceof RSIHTTPError); assert.equal(choice.code, 'recipe_choice_required'); assert.deepEqual(choice.recipeIds, ['weekly_recipe', 'weekly_short']);
  const incomplete: any = await a.startExecution(weekly.task_id, { key: 'e3', source: 'external_artifact', artifacts: ['output'] }).then(() => undefined, value => value);
  assert.equal(incomplete.code, 'input_incomplete'); assert.deepEqual(incomplete.missing, ['objective', 'original_input_or_self_contained_task']);
  assert.deepEqual(ServiceFake.input('external_artifact', { objective: 'x' })[1], ['original_input_or_self_contained_task', 'submitted_output']);
});

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
  assert.equal(learning.max_completion_tokens, DEFAULT_BUDGET); assert.deepEqual(service.requests.at(-1)!.body, {});
  assert.equal(wrapper.cosens.getTurn(wrapper.cosens.lastTurnId)?.learning?.execution_id, learning.execution_id);
  service.learnings.set(learning.execution_id, { ...service.learnings.get(learning.execution_id)!, status: 'interrupted' });
  const error: any = await client.atoms.waitLearning(learning.execution_id, { timeout: 1 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSILearningStoppedError); assert.equal(error.status, 'interrupted');
  assert.equal((await client.atoms.resumeLearning(learning.execution_id)).status, 'queued');
  assert.equal((await client.atoms.waitLearning(learning.execution_id, { timeout: 1 })).status, 'completed');
  await wrapper.close();
});

test('the fake follows the service: keys, feedback, request hashes, a frozen model and Recipe lookups', async () => {
  const service = new ServiceFake();
  service.addTask('Produce output');
  const recipe = (version: number, origin = 'generated') => ({ recipe_id: 'recipe_1', origin, version, task_id: 'task_1', recipe: { title: `Report v${version}`, steps: [] } });
  service.recipes = [recipe(1), recipe(3), { recipe_id: 'team', origin: 'uploaded', version: 1, recipe: { title: 'Team' } }];
  const a = new CoSensClient({ apiKey: 'test-key', fetch: service.fetch }).atoms;
  const status = (error: any) => error.statusCode;
  const first = await a.startExecution('task_1', { key: 'same', model: 'm1', ...NEW });
  assert.equal(first.request.method.version, 3);
  assert.equal((await a.startExecution('task_1', { key: 'same', ...NEW })).execution_id, first.execution_id);
  for (const changed of [{ recallMemory: true }, { objective: 'Another week' }]) {
    await assert.rejects(a.startExecution('task_1', { key: 'same', ...NEW, ...changed }), (e: any) => status(e) === 422 && /different execution/.test(e.message));
  }
  // One key space across tasks: a key the first task's start took is refused in another task.
  service.addTask('Produce another output');
  await assert.rejects(a.startExecution('task_2', { key: 'same', ...NEW }), (e: any) => status(e) === 422 && /different execution/.test(e.message));
  await assert.rejects(a.startExecution('task_1', { key: 'other-model', model: 'm2', ...NEW }), (e: any) => status(e) === 422 && /frozen/.test(e.message));
  await assert.rejects(a.submitExecution(first.execution_id, { requestSha256: 'stale', outcome: 'completed', artifacts: [{ artifact_id: 'a1', role: 'submitted_output' }] }), /request hash mismatch/);
  await assert.rejects(a.startExecution('task_1', { key: 'early', previousExecutionId: first.execution_id }), (e: any) => status(e) === 422 && /no completed output/.test(e.message));
  await a.submitExecution(first.execution_id, { requestSha256: first.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: 'a1', role: 'submitted_output' }] });
  const second = await a.startExecution('task_1', { key: 'other', ...NEW });
  await a.submitExecution(second.execution_id, { requestSha256: second.request_sha256, outcome: 'completed', artifacts: [{ artifact_id: 'a2', role: 'submitted_output' }] });
  // A start key belongs to one execution start: a correction replays its own key and refuses another start's.
  const fixFirst = await a.startExecution('task_1', { key: 'fix', previousExecutionId: first.execution_id });
  assert.deepEqual(await a.startExecution('task_1', { key: 'fix', previousExecutionId: first.execution_id }), fixFirst);
  for (const [key, previous] of [['fix', second.execution_id], ['other', first.execution_id]]) {
    await assert.rejects(a.startExecution('task_1', { key, previousExecutionId: previous }), (e: any) => status(e) === 422 && /different execution/.test(e.message));
  }
  await assert.rejects(a.startExecution('task_1', { key: 'fix', ...NEW }), (e: any) => status(e) === 422 && /different execution/.test(e.message));
  const fixSecond = await a.startExecution('task_1', { key: 'fix-second', previousExecutionId: second.execution_id });
  assert.equal(new Set([first, second, fixFirst, fixSecond].map(x => x.execution_id)).size, 4);
  assert.equal(fixFirst.request.method.version, 3);
  for (const [taskId, previous, code] of [['task_1', 'execution_99', 404], ['task_2', first.execution_id, 422]] as const) {
    await assert.rejects(a.startExecution(taskId, { key: 'fix', previousExecutionId: previous }), (e: any) => status(e) === code);
  }
  await a.recordFeedback(first.execution_id, { feedback: 'Too long', source: 'human', key: 'fb' });
  assert.equal((await a.recordFeedback(first.execution_id, { feedback: 'Too long', source: 'human', key: 'fb' })).message, 'Too long');
  await assert.rejects(a.recordFeedback(first.execution_id, { feedback: 'Too short', source: 'human', key: 'fb' }), (e: any) => status(e) === 422 && /different feedback/.test(e.message));
  await assert.rejects(a.learn(first.execution_id, { model: 'm2' }), /frozen/);
  assert.equal((await a.learn(first.execution_id, { model: 'm1' })).max_completion_tokens, DEFAULT_BUDGET);
  assert.equal((await a.getRecipe('recipe_1')).version, 3);
  assert.equal((await a.getRecipe('recipe_1', { version: 1 })).version, 1);
  assert.equal((await a.getRecipe('team')).task_id, null);
  await assert.rejects(a.getRecipe('recipe_1', { version: 9 }), (e: any) => status(e) === 404);
  await assert.rejects(a.getRecipe('missing'), (e: any) => status(e) === 404);
  assert.deepEqual((await a.listRecipes({ limit: 100, origin: 'generated' })).items.map((x: any) => [x.recipe_id, x.version, x.task_id]), [['recipe_1', 3, 'task_1']]);
  assert.deepEqual((await a.searchRecipes('report v3')).items.map((x: any) => x.recipe_id), ['recipe_1']);
  assert.deepEqual((await a.searchRecipes('nothing')).items, []);
  const task = await a.getTask('task_1');
  assert.deepEqual(task.recipes, [{ recipe_id: 'recipe_1', current_version: 3, latest_version: 3, learning: null }]);
  assert.deepEqual(task.executions.map((x: any) => [x.kind, x.instance_id]), [['original', first.execution_id], ['original', second.execution_id],
    ['correction', first.execution_id], ['correction', second.execution_id]]);
});
