import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { RSIClient } from '../src/rsi/index.js';
import type { Execution, Question } from '../src/rsi/models.js';
import { Store } from '../src/rsi/store.js';

const question = (id: string, revision: number): Question => ({
  id,
  revision,
  question: `Choose for ${id}`,
  options: [{ id: 'a', label: 'A' }, { id: 'b', label: 'B' }],
  assumption: 'Assume A',
});

test('Task restart follows legacy clarification and preserves the prior attempts', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'rsi-task-'));
  const client = new RSIClient({
    data_dir, model_id: 'fixture', base_url: 'http://127.0.0.1:1/v1', api_key: 'fixture',
    config: { auto_start: false },
  });
  const store = new Store(data_dir);
  t.after(() => {
    store.close();
    client.close();
    rmSync(data_dir, { recursive: true, force: true });
  });
  const context_path = join(data_dir, 'context.md');
  const path = join(data_dir, 'RECIPE.md');
  const originalInput = join(data_dir, 'original-input.csv');
  const newInput = join(data_dir, 'new-input.csv');
  writeFileSync(context_path, 'A task');
  writeFileSync(path, '---\nname: Sample\ndescription: Test Recipe\n---\nMake the report.\n');
  writeFileSync(originalInput, 'col\noriginal\n');
  writeFileSync(newInput, 'col\nexample\n');
  const recipe = await client.recipe_insert(path);
  const recipe_reference = { path, recipe_id: recipe.id };
  const first = await client.execution_create({
    evidence: { context_path, files: [{ path: originalInput, role: 'input' }] },
    idempotency_key: 'initial', recipe_reference,
  });
  const second = await client.execution_create({
    evidence: { context_path, files: [] }, idempotency_key: 'separate-update', recipe_reference,
  });
  const stale = question('stale', 1);
  store.save({ ...store.get<Execution>(second.id, 'execution'), questions: [stale] });
  await client.execution_append_evidence(second.id, {
    context_path, files: [{ path: newInput, role: 'reference' }],
  });
  await assert.rejects(client.execution_clarification_answer(second.id, {
    question_id: stale.id, question_revision: stale.revision, answer: ['a'],
  }), /missing or stale/);
  assert.equal((await client.task_get(second.id)).pending_question_count, 0);
  const firstQuestion = question('first', 1);
  store.save({ ...store.get<Execution>(first.id, 'execution'), status: 'running', questions: [firstQuestion] });
  const reply = await client.execution_clarification_answer(first.id, {
    question_id: firstQuestion.id, question_revision: firstQuestion.revision, answer: ['a'],
  });
  assert.ok(reply.continuation_execution_id);
  const legacyId = reply.continuation_execution_id;
  store.save({ ...store.get<Execution>(first.id, 'execution'), continuation_execution_id: undefined, questions: [] });
  const legacy = store.get<Execution>(legacyId, 'execution');
  const secondQuestion = question('second', legacy.input_revision);
  const error = { code: 'timeout', type: 'fixture', stage: 'analyze' };
  store.save({ ...legacy, status: 'failed', questions: [secondQuestion], error });

  const failed = await client.task_get(first.id);
  assert.deepEqual(failed.execution_ids, [first.id, legacyId]);
  assert.deepEqual(failed.pending_questions, []);
  assert.deepEqual(failed.clarification_history.map((entry) => [entry.question_id, entry.answer?.selected_option_ids]), [
    ['first', ['a']], ['second', undefined],
  ]);
  assert.deepEqual(failed.error, error);
  assert.equal(failed.artifact, null);
  assert.deepEqual(new Set((await client.task_list()).map(({ task_id }) => task_id)), new Set([second.id, first.id]));

  await assert.rejects(client.task_restart(first.id, {
    idempotency_key: 'invalid-restart',
    evidence: { context_path, files: [{ path: originalInput, role: 'feedback' }] },
  }), /cannot also have a learning-only role/);
  assert.deepEqual((await client.task_get(first.id)).execution_ids, [first.id, legacyId]);

  const options = { idempotency_key: 'restart', evidence: { context_path, files: [{ path: newInput, role: 'input' as const }] } };
  const restarted = await client.task_restart(first.id, options);
  assert.notEqual(restarted.execution_id, legacyId);
  assert.deepEqual(await client.task_restart(first.id, options), restarted);
  assert.deepEqual(store.get<Execution>(legacyId, 'execution').error, error);
  const queued = store.get<Execution>(restarted.execution_id, 'execution');
  assert.equal(queued.recipe_id, recipe.id);
  assert.equal(queued.status, 'queued');
  assert.equal(queued.recipe_version_id, null);
  assert.equal(queued.material_ids.length, 2);
  assert.equal(queued.policy.test_task?.input_locators?.length, 2);
  assert.ok(queued.answers['first:1']);
  assert.deepEqual(queued.questions, []);

  store.save({ ...queued, status: 'running' });
  const runningAgain = await client.task_restart(first.id, { idempotency_key: 'restart-again' });
  assert.equal(store.get<Execution>(queued.id, 'execution').status, 'cancelled');
  const detail = await client.task_get(first.id);
  assert.deepEqual(detail.execution_ids, [first.id, legacyId, queued.id, runningAgain.execution_id]);
  assert.equal(detail.execution_id, runningAgain.execution_id);
  assert.equal((await client.task_restart(first.id, options)).execution_id, queued.id);
  assert.equal((await client.task_get(second.id)).execution_id, second.id);
});
