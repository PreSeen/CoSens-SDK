import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import * as sdk from '../src/index.ts';
import { CoSensClient, RSIError, RSIHTTPError, RSILearningFailedError, RSILearningStoppedError, RSIRecoveryRequired } from '../src/index.ts';
import * as http from '../src/http.ts';
import { clock } from '../src/clock.ts';

const REMOVED = ['getRevision', 'startRevision', 'startRecipeCandidate', 'getRecipeCandidate', 'resumeRecipeCandidate',
  'listRecipeCandidates', 'waitWorkflowJob', 'recallContext', 'startSession', 'appendSessionEvents', 'getSessionContext', 'endSession'];
const DETAIL = { code: 'upstream_unavailable', message: 'The model gateway was unavailable.', retryable: true,
  phase: 'analyzing', upstream_status: 503, upstream_code: 'gateway_unavailable' };

function learning(status: string, fields: Record<string, unknown> = {}) {
  return { execution_id: 'e1', recipe_id: 'r1', version: 1, previous_version: null, current: false, status, phase: 'queued',
    attribution: null, recipe: null, recipe_sha256: null, error: null, error_detail: null, models: null,
    memory_context: 'captured', initial: true, material_gap: null, max_completion_tokens: 200_000, ...fields };
}

/** Records [method, path, body, idempotency key] and each query; serves the execution view from `views` in order. */
function server(views: any[] = []) {
  const seen: Array<[string, string, any, string | null]> = [];
  const queries: Array<Record<string, string>> = [];
  const queue = [...views];
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    const body = init?.body === undefined ? undefined : JSON.parse(String(init.body));
    seen.push([init?.method ?? 'GET', url.pathname, body, new Headers(init?.headers).get('idempotency-key')]);
    queries.push(Object.fromEntries(url.searchParams));
    if ((init?.method ?? 'GET') === 'GET' && url.pathname === '/api/executions/e1') {
      const view = queue.length > 1 ? queue.shift() : queue[0];
      return new Response(JSON.stringify({ execution_id: 'e1', task_id: 't1', learning: view }), { headers: { 'x-request-id': 'req-9' } });
    }
    return new Response(JSON.stringify({ execution_id: 'e1', status: 'queued' }));
  };
  return { seen, queries, client: new CoSensClient({ apiKey: 'test-key', fetch: fetcher }) };
}

let sleeps: number[] = [];
let now = 0;
const real = { ...clock };
beforeEach(() => {
  sleeps = []; now = 0;
  clock.now = () => now;
  clock.sleep = async ms => { sleeps.push(ms); now += ms; };
});
afterEach(() => { Object.assign(clock, real); });

test('startExecution sends a correction with previousExecutionId', async () => {
  const { seen, client } = server();
  await client.atoms.startExecution(undefined, { key: 'k', previousExecutionId: 'e0' });
  assert.deepEqual(seen, [['POST', '/api/executions', { previous_execution_id: 'e0' }, 'k']]);
  await client.atoms.startExecution(undefined, { key: 'k2', previousExecutionId: 'e0', model: 'm' });
  assert.deepEqual(seen[1]![2], { model: 'm', previous_execution_id: 'e0' });
});

test('a correction may name its task', async () => {
  const { seen, client } = server();
  await client.atoms.startExecution('t1', { key: 'k', previousExecutionId: 'e0' });
  assert.deepEqual(seen, [['POST', '/api/executions', { task_id: 't1', previous_execution_id: 'e0' }, 'k']]);
});

for (const extra of [{ recipeId: 'r' }, { recallMemory: true }, { source: 'new_task' }, { objective: 'w1' }, { artifacts: [] }, { selfContained: false }]) {
  test(`a correction rejects ${Object.keys(extra)[0]} before sending`, async () => {
    const { seen, client } = server();
    assert.throws(() => client.atoms.startExecution('t1', { key: 'k', previousExecutionId: 'e0', ...extra } as any),
      /a correction takes only previousExecutionId, taskId and model/);
    assert.deepEqual(seen, []);
  });
}

test('a new execution carries its own input', async () => {
  const { seen, client } = server();
  await client.atoms.startExecution('t1', { key: 'k', source: 'new_task', objective: 'w1', selfContained: true });
  assert.deepEqual(seen, [['POST', '/api/executions', { task_id: 't1', source: 'new_task', input: { artifacts: [], objective: 'w1', self_contained: true } }, 'k']]);
  await client.atoms.startExecution('t1', { key: 'k2', source: 'external_artifact', artifacts: ['out', { artifact_id: 'in', role: 'original_input' }], recipeId: 'r1', recallMemory: true, model: 'm' });
  assert.deepEqual(seen[1]![2], { model: 'm', task_id: 't1', source: 'external_artifact', recall_memory: true, recipe_id: 'r1',
    input: { artifacts: ['out', { artifact_id: 'in', role: 'original_input' }] } });
});

test('a new execution needs taskId and source', async () => {
  const { seen, client } = server();
  assert.throws(() => client.atoms.startExecution('t1', { key: 'k' } as any), /a new execution needs taskId and source/);
  assert.throws(() => client.atoms.startExecution(undefined, { key: 'k', source: 'new_task', objective: 'w1' }), /a new execution needs taskId and source/);
  assert.deepEqual(seen, []);
});

for (const [options, message] of [
  [{ source: 'external_artifact', artifacts: [{ artifact_id: 'a1', role: 'original_input' }] }, /external_artifact requires submitted_output/],
  [{ source: 'bogus' }, /source must be new_task or external_artifact/],
  [{ source: 'new_task', objective: ' ' }, /objective must be nonempty text of at most 20,000 characters/],
  [{ source: 'new_task', artifacts: [{ artifact_id: 'a1', role: 'reference' }] }, /invalid artifact reference or role/],
  [{ source: 'new_task', artifacts: ['a1', { artifact_id: 'a1', role: 'submitted_output' }] }, /one artifact cannot be both original_input and submitted_output/],
  [{ source: 'new_task', selfContained: 'yes' }, /selfContained must be a boolean/],
] as const) {
  test(`a new execution input is checked before sending: ${message.source}`, async () => {
    const { seen, client } = server();
    assert.throws(() => client.atoms.startExecution('t1', { key: 'k', ...options } as any), message);
    assert.deepEqual(seen, []);
  });
}

test('startExecution checks the key before the body', async () => {
  const { seen, client } = server();
  assert.throws(() => client.atoms.startExecution('t1', { key: '', source: 'bogus' } as any), /key must be/);
  assert.deepEqual(seen, []);
});

test('createTask sends the capability', async () => {
  const { seen, client } = server();
  await client.atoms.createTask({ key: 'k', objective: 'Weekly report', artifacts: ['a1', { artifact_id: 'a2', role: 'reference' }] });
  assert.deepEqual(seen, [['POST', '/api/tasks', { objective: 'Weekly report', artifacts: ['a1', { artifact_id: 'a2', role: 'reference' }] }, 'k']]);
});

for (const [options, message] of [
  [{ objective: 'w', artifacts: [{ artifact_id: 'a1', role: 'original_input' }] }, /a task's artifacts are ids or references with role reference/],
  [{ objective: '' }, /objective must be nonempty/],
  [{ objective: '   ' }, /objective must be nonempty/],
  [{ objective: 'x'.repeat(20_001) }, /objective must be nonempty/],
  [{ objective: 'w', artifacts: Array.from({ length: 101 }, (_, n) => `a${n}`) }, /artifacts must contain at most 100 references/],
] as const) {
  test(`createTask rejects ${message.source} before sending`, async () => {
    const { seen, client } = server();
    assert.throws(() => client.atoms.createTask({ key: 'k', ...options } as any), message);
    assert.deepEqual(seen, []);
  });
}

test('supplementTaskContext sends reference artifacts and the expected version', async () => {
  const { seen, client } = server();
  await client.atoms.supplementTaskContext('t1', { expectedContextVersion: 2, objective: 'Weekly report', artifacts: ['a1'] });
  await client.atoms.supplementTaskContext('t1', { expectedContextVersion: 3 });
  assert.deepEqual(seen.map(entry => [entry[1], entry[2]]), [
    ['/api/tasks/t1/context', { expected_context_version: 2, objective: 'Weekly report', artifacts: ['a1'] }],
    ['/api/tasks/t1/context', { expected_context_version: 3 }]]);
  assert.throws(() => client.atoms.supplementTaskContext('t1', { expectedContextVersion: 0 }), /expectedContextVersion must be a positive integer/);
  assert.throws(() => client.atoms.supplementTaskContext('t1', { expectedContextVersion: 1, artifacts: [{ artifact_id: 'a1', role: 'acceptance' }] } as any), /role reference/);
  assert.equal(seen.length, 2);
});

test('recordFeedback is keyed by execution', async () => {
  const { seen, client } = server();
  await client.atoms.recordFeedback('e1', { feedback: 'f', source: 'human', key: 'kf' });
  assert.deepEqual(seen, [['POST', '/api/executions/e1/feedback', { feedback: 'f', source: 'human' }, 'kf']]);
});

test('learn and resumeLearning send no idempotency key', async () => {
  const { seen, client } = server();
  await client.atoms.learn('e1', { maxCompletionTokens: 200_000 });
  await client.atoms.learn('e1');
  await client.atoms.resumeLearning('e1', { model: 'm' });
  await client.atoms.resumeLearning('e1');
  assert.deepEqual(seen, [
    ['POST', '/api/executions/e1/learn', { max_completion_tokens: 200_000 }, null],
    ['POST', '/api/executions/e1/learn', {}, null],
    ['POST', '/api/executions/e1/resume', { model: 'm' }, null],
    ['POST', '/api/executions/e1/resume', undefined, null],
  ]);
});

test('an invalid learning budget is rejected before sending', async () => {
  const { seen, client } = server();
  assert.throws(() => client.atoms.learn('e1', { maxCompletionTokens: 0 }), /maxCompletionTokens/);
  assert.throws(() => client.atoms.resumeLearning('e1', { maxCompletionTokens: 1_000_001 }), /maxCompletionTokens/);
  assert.deepEqual(seen, []);
});

test('waitLearning returns the completed learning with the Python backoff', async () => {
  const { seen, client } = server([learning('queued'), learning('running'), learning('completed', { current: true })]);
  const value = await client.atoms.waitLearning('e1', { timeout: 60 });
  assert.equal(value.status, 'completed'); assert.equal(value.current, true);
  assert.equal(seen.length, 3);
  assert.deepEqual(sleeps, [2000, 4000]);
});

test('waitLearning never sleeps past the deadline', async () => {
  const { client } = server([learning('queued'), learning('completed')]);
  await client.atoms.waitLearning('e1', { timeout: 0.5 });
  assert.deepEqual(sleeps, [500]);
});

test('waitLearning throws a typed failure from error_detail', async () => {
  const { client } = server([learning('queued'), learning('failed', { error: 'upstream_unavailable', error_detail: DETAIL })]);
  const error: any = await client.atoms.waitLearning('e1', { timeout: 60 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSILearningFailedError); assert.ok(!(error instanceof RSILearningStoppedError));
  assert.deepEqual([error.executionId, error.code, error.retryable, error.phase, error.upstreamStatus, error.upstreamCode, error.requestId],
    ['e1', 'upstream_unavailable', true, 'analyzing', 503, 'gateway_unavailable', 'req-9']);
  assert.equal(error.message, 'Learning failed for execution e1: upstream_unavailable');
  assert.equal(error.detailMessage, DETAIL.message);
});

test('fromLearning keeps retryable unknown without error_detail and ignores a non-object detail', () => {
  const bare = RSILearningFailedError.fromLearning(learning('failed', { error: 'previous_version_failed', phase: 'generating' }));
  assert.deepEqual([bare.code, bare.retryable, bare.phase, bare.detailMessage], ['previous_version_failed', undefined, 'generating', 'previous_version_failed']);
  assert.equal(RSILearningFailedError.fromLearning({ execution_id: 'e1', status: 'failed', error: 'x' }).retryable, undefined);
  assert.equal(RSILearningFailedError.fromLearning(learning('failed', { error: 'x', error_detail: 'bare' })).code, 'x');
  assert.equal(RSILearningFailedError.fromLearning(learning('failed', { error_detail: { ...DETAIL, retryable: false } })).retryable, false);
});

for (const status of ['interrupted', 'awaiting_runner', 'awaiting_material']) {
  test(`waitLearning stops on ${status}, which only the caller can change`, async () => {
    const view = learning(status, status === 'awaiting_material' ? { material_gap: 'no_actionable_feedback' } : {});
    const { seen, client } = server([learning('queued'), view]);
    const error: any = await client.atoms.waitLearning('e1', { timeout: 60 }).then(() => undefined, value => value);
    assert.ok(error instanceof RSILearningStoppedError); assert.ok(error instanceof RSIError); assert.ok(!(error instanceof RSILearningFailedError));
    assert.deepEqual([error.status, error.executionId, error.requestId], [status, 'e1', 'req-9']);
    assert.deepEqual(error.learning, view);
    assert.equal(seen.length, 2);
  });
}

test('the stopped error leaves out an empty material gap', () => {
  assert.ok(!new RSILearningStoppedError(learning('awaiting_material')).message.includes('()'));
  assert.ok(new RSILearningStoppedError(learning('awaiting_material', { material_gap: 'no_actionable_feedback' })).message.includes('(no_actionable_feedback)'));
  assert.equal(new RSILearningStoppedError(learning('paused')).status, 'paused');
});

test('waitLearning times out on a learning still in flight', async () => {
  const { client } = server([learning('awaiting_previous_version')]);
  await assert.rejects(client.atoms.waitLearning('e1', { timeout: 0 }), (error: any) => error.name === 'TimeoutError' && /e1/.test(error.message));
});

test('waitLearning rejects an unknown status or a missing learning', async () => {
  await assert.rejects(server([learning('paused')]).client.atoms.waitLearning('e1', { timeout: 1 }), (error: any) => error instanceof RSIRecoveryRequired && /paused/.test(error.message));
  await assert.rejects(server([null]).client.atoms.waitLearning('e1', { timeout: 1 }), (error: any) => error instanceof RSIRecoveryRequired && /learn/.test(error.message));
});

test('the Recipe store atoms send previous_version and the exact query', async () => {
  const { seen, queries, client } = server();
  const recipe = { title: 'T', when: 'W', when_not: 'N', steps: [{ id: 's1', instruction: 'I' }], lessons: [] };
  await client.atoms.uploadRecipe('team_recipe-1', { version: 4, recipe, previousVersion: 3 });
  await client.atoms.uploadRecipe('team_recipe-1', { version: 1, recipe });
  await client.atoms.getRecipe('g1', { version: 2 });
  await client.atoms.listRecipes({ limit: 5, cursor: 'c1', origin: 'generated' });
  await client.atoms.listRecipes();
  await client.atoms.searchRecipes('sort words', { limit: 3 });
  assert.deepEqual(seen.map(([method, path, body]) => [method, path, body]), [
    ['PUT', '/api/recipes/team_recipe-1/versions/4', { previous_version: 3, recipe }],
    ['PUT', '/api/recipes/team_recipe-1/versions/1', { recipe }],
    ['GET', '/api/recipes/g1', undefined],
    ['GET', '/api/recipes', undefined],
    ['GET', '/api/recipes', undefined],
    ['GET', '/api/recipes', undefined],
  ]);
  assert.deepEqual(queries, [{}, {}, { version: '2' }, { limit: '5', cursor: 'c1', origin: 'generated' }, { limit: '20' }, { q: 'sort words', limit: '3' }]);
  await client.atoms.searchRecipes('sort words');
  await client.atoms.getRecipe('g1');
  assert.deepEqual(queries.slice(-2), [{ q: 'sort words', limit: '10' }, {}]);
  for (const call of [() => client.atoms.uploadRecipe('a/b', { version: 1, recipe }), () => client.atoms.uploadRecipe('r', { version: 0, recipe }),
    () => client.atoms.uploadRecipe('r', { version: 2, recipe, previousVersion: 0 }), () => client.atoms.listRecipes({ origin: 'copied' as any }),
    () => client.atoms.listRecipes({ limit: 101 }), () => client.atoms.searchRecipes(''), () => client.atoms.searchRecipes('x', { limit: 51 })]) {
    assert.throws(call, TypeError);
  }
});

test('setCurrentRecipeVersion sends the expected current version without an idempotency key', async () => {
  const { seen, client } = server();
  await client.atoms.setCurrentRecipeVersion('r1', { version: 2, expectedCurrentVersion: 3 });
  assert.deepEqual(seen, [['PUT', '/api/recipes/r1/current', { version: 2, expected_current_version: 3 }, null]]);
  for (const call of [() => client.atoms.setCurrentRecipeVersion('', { version: 2, expectedCurrentVersion: 1 }),
    () => client.atoms.setCurrentRecipeVersion('r1', { version: 0, expectedCurrentVersion: 1 }),
    () => client.atoms.setCurrentRecipeVersion('r1', { version: 2, expectedCurrentVersion: 1.5 })]) {
    assert.throws(call, TypeError);
  }
  assert.equal(seen.length, 1);
});

test('a stale expectedCurrentVersion is a 409 with the current version', async () => {
  const client = new CoSensClient({ apiKey: 'test-key', fetch: async () => new Response(JSON.stringify(
    { error: { code: 'recipe_version_conflict', message: 'the current version changed', current_version: 2 } }), { status: 409 }) });
  const error = await client.atoms.setCurrentRecipeVersion('r1', { version: 1, expectedCurrentVersion: 3 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSIHTTPError);
  assert.equal(error.statusCode, 409);
  assert.equal(error.code, 'recipe_version_conflict');
  assert.equal(error.currentVersion, 2);
});

test('removed atoms, errors and helpers are gone', async () => {
  const { client } = server();
  assert.deepEqual(REMOVED.filter(name => name in client.atoms), []);
  assert.equal('RSICandidateFailedError' in sdk, false);
  assert.equal('recipeRequestKey' in http, false);
});
