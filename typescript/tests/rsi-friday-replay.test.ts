import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test, { type TestContext } from 'node:test';
import { RSIClient as WorkerClient } from '../src/rsi/client.js';
import { RSIClient } from '../src/rsi/index.js';
import type { Execution, Runtime } from '../src/rsi/models.js';

const original = '保持顺序并保存到 todo.md：\n确认销售额\n核对退款金额';
const prior = '保持顺序并保存到 prior.md：\n查询历史订单\n核对历史发票';
const changed = '保持顺序并保存到 changed.md：\n修改磁盘输入\n忽略原始请求';
const explicit = '保持顺序并保存到 explicit.md：\n检查库存\n核对发货数量';

function user(text: string, metadata = { 'friday.client': 'desktop' }) {
  return { id: 'source-user', type: 'user', metadata, time: { created: 3 }, text, files: [] };
}

function assistant(id: string, text: string) {
  return { id, type: 'assistant', finish: 'stop', time: { completed: 5 }, content: [{ type: 'text', text }] };
}

function fridayContext(current: ReturnType<typeof user>) {
  return JSON.stringify({
    version: 1,
    messages: [
      { ...user(prior), id: 'earlier-user' },
      assistant('earlier-reply', 'Older assistant reply.'),
      { id: 'idle', type: 'idle', outcome: 'succeeded' },
      current,
      assistant('source-reply', 'Historical assistant answer.'),
    ],
    feedback: 'Historical feedback.',
  });
}

function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), 'rsi-friday-replay-'));
  const client = new RSIClient({
    data_dir: directory,
    model_id: 'fixture',
    base_url: 'http://127.0.0.1:1/v1',
    api_key: 'fixture',
    config: { auto_start: false },
  });
  const worker = new WorkerClient({ data_dir: directory, auto_start: false });
  t.after(() => {
    client.close();
    worker.close();
    rmSync(directory, { recursive: true, force: true });
  });

  const context_path = join(directory, 'context.json');
  const artifact = join(directory, 'historical-output.md');
  writeFileSync(artifact, 'Historical file content.');
  const evidence = { context_path, files: [{ path: artifact, role: 'artifact' as const }] };
  const replays: { instruction: string; roles: string[] }[] = [];
  const runtime: Runtime = {
    async analyze() {
      return {
        recipe_name: 'Organize a checklist',
        sufficient_information: true,
        rubrics: { criteria: [{ id: 'order', description: 'Preserve list order', source: 'agent_inference' }] },
      };
    },
    async generate() {
      return {
        name: 'Organize a checklist',
        description: 'Write a checklist in the requested order.',
        instructions: 'Use the supplied request to determine the list and destination.',
      };
    },
    async execute(context, files, workspace) {
      const { instruction } = context.test_task as { instruction: string };
      replays.push({ instruction, roles: files.map(({ role }) => role) });
      const firstInput = files.find(({ role }) => role === 'input');
      const request = firstInput ? readFileSync(firstInput.snapshot, 'utf8') : instruction;
      const details = /保存到 ([\w-]+\.md)：\n([^\n]+)\n([^\n]+)/u.exec(request);
      if (details) {
        const [, path, first, second] = details;
        writeFileSync(join(workspace, path), `- [ ] ${first}\n- [ ] ${second}\n`);
      }
      return {};
    },
    async assess(context, files) {
      const { instruction } = context.test_task as { instruction: string };
      assert.equal(instruction, replays.at(-1)?.instruction);
      assert.ok(files.every(({ role }) => role === 'artifact' || role === 'input'));
      const artifact = files.find(({ role }) => role === 'artifact');
      assert.ok(artifact);
      return {
        verifiable: true,
        scores: [{ criterion_id: 'order', score: 1, evidence: artifact.locator }],
        reason: 'The replay produced the checklist.',
      };
    },
  };

  return { client, worker, evidence, replays, run: () => worker.run_pending({ runtime }) };
}

test('Friday replay gives the executor the anchored frozen request for new tasks and legacy retries', async (t) => {
  const { client, worker, evidence, replays, run } = fixture(t);
  writeFileSync(evidence.context_path, fridayContext(user(original)));
  const request = { evidence, idempotency_key: 'friday-authoring:new' };
  const execution = await client.execution_create(request);
  const legacy = await client.execution_create({ evidence, idempotency_key: 'friday-authoring:legacy' });
  writeFileSync(evidence.context_path, fridayContext(user(changed)));
  await client.execution_append_evidence(legacy.id, { context_path: evidence.context_path, files: [] });
  worker.store.save({ ...worker.store.get<Execution>(legacy.id, 'execution'), status: 'failed', stage: 'failed' });

  assert.equal((await client.execution_create(request)).id, execution.id);
  const retryOptions = { idempotency_key: 'friday-authoring-retry:legacy' };
  const retry = await client.task_restart(legacy.id, retryOptions);
  assert.notEqual(retry.execution_id, legacy.id);
  assert.equal((await client.task_restart(legacy.id, retryOptions)).execution_id, retry.execution_id);
  assert.equal((await client.execution_get(legacy.id)).status, 'failed');
  assert.deepEqual(await run(), { processed: 2 });

  assert.equal(replays.length, 2);
  for (const { instruction, roles } of replays) {
    assert.deepEqual(roles, []);
    assert.ok(instruction.includes(original));
    for (const excluded of [prior, changed, 'Historical assistant answer.', 'Historical feedback.', 'Historical file content.']) {
      assert.ok(!instruction.includes(excluded));
    }
  }
  for (const id of [execution.id, retry.execution_id]) {
    const result = await client.execution_get(id);
    assert.equal(result.status, 'completed', JSON.stringify(result.error));
    const [replayed] = result.test_results;
    assert.equal(replayed.status, 'passed');
    assert.deepEqual(replayed.artifacts.map(({ locator }) => locator), ['test/todo.md']);
    assert.equal(readFileSync(replayed.artifacts[0].path, 'utf8'), '- [ ] 确认销售额\n- [ ] 核对退款金额\n');
  }
});

test('Friday replay respects the original entry point, authoring commands, and explicit input', async (t) => {
  const { client, evidence, replays, run } = fixture(t);
  writeFileSync(evidence.context_path, fridayContext(user(original)));
  // The context shape alone does not authorize use of the supplied message.
  const unrelated = await client.execution_create({ evidence, idempotency_key: 'other:friday-authoring:fixture' });
  const withText = await client.execution_create({
    evidence: { ...evidence, input_text: explicit }, idempotency_key: 'friday-authoring:explicit',
  });
  const commandMetadata = { 'friday.client': 'desktop', 'friday.intent': 'recipe-authoring' };
  writeFileSync(evidence.context_path, fridayContext(user(`/rsi ${original}`, commandMetadata)));
  const command = await client.execution_create({ evidence, idempotency_key: 'friday-authoring:command' });
  assert.deepEqual(await run(), { processed: 3 });

  assert.equal(replays.length, 3);
  for (const { instruction } of replays) {
    assert.ok(!instruction.includes(original));
    assert.ok(!instruction.includes(prior));
  }
  assert.deepEqual(replays.filter(({ roles }) => roles.length > 0).map(({ roles }) => roles), [['input']]);
  for (const id of [unrelated.id, command.id]) {
    assert.equal((await client.execution_get(id)).test_results[0].status, 'unverifiable');
  }
  const [replayed] = (await client.execution_get(withText.id)).test_results;
  assert.equal(replayed.status, 'passed');
  assert.deepEqual(replayed.artifacts.map(({ locator }) => locator), ['test/explicit.md']);
});
