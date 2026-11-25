import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { RSIClient } from '../src/rsi/client.js';
import { readRecipePackage, stageRecipePackageFiles } from '../src/rsi/recipe-package.js';
import { readTask } from '../src/rsi/task.js';

test('local update keeps Recipe resources through generation, testing, and the frozen ZIP', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-resources-'));
  const client = new RSIClient({
    data_dir, config: { model: { model: 'fixture', base_url: 'https://model.example/v1' } },
    auto_start: false,
  });
  t.after(() => {
    client.close();
    rmSync(data_dir, { recursive: true, force: true });
  });
  const context_path = join(data_dir, 'context.md');
  writeFileSync(context_path, 'Produce the deliverable.');
  const content = { name: 'Report', description: 'Make a report', instructions: 'Write a report.' };
  const base = client.recipe_insert(content, '---\nname: Report\ndescription: Make a report\n---\nWrite a report.\n');
  const resources = new Map([
    ['assets/data.bin', Buffer.from([0, 255])],
    ['scripts/report.py', Buffer.from('print("fixture")\n')],
    ['templates/example.txt', Buffer.from('Reusable template\n')],
  ]);
  stageRecipePackageFiles(join(data_dir, base.storage_path, base.active_version_id!), resources);
  const material = client.material_create({ manifest: { context_path } });
  const execution = await client.execution_create({
    material_ids: [material.id],
    base_recipe_version_id: base.active_version_id,
    policy: {
      artifact_confirmed: true,
      confirmation_ref: 'fixture',
      test_task: { instruction: 'Create answer.txt.', checks: [{ kind: 'exists', path: 'answer.txt' }] },
    },
  });

  assert.deepEqual(await client.run_pending({
    runtime: {
      async analyze(_context, files) {
        return {
          sufficient_information: true,
          questions: [],
          rubrics: { criteria: [{
            id: 'correct', description: 'Create the required answer.', source: 'task',
            evidence: [{ material_id: material.id, locator: files[0].locator }],
          }] },
        };
      },
      async generate(context) {
        assert.deepEqual(context.base_recipe, content);
        assert.deepEqual(context.base_recipe_resources, [
          { path: 'assets/data.bin', size: 2 },
          { path: 'scripts/report.py', size: 17, text: 'print("fixture")\n' },
          { path: 'templates/example.txt', size: 18, text: 'Reusable template\n' },
        ]);
        return { ...content, instructions: 'Create answer.txt from the template.' };
      },
      async execute(context, _files, workspace, trace) {
        assert.deepEqual(context.package_files, [...resources.keys()]);
        assert.deepEqual(readFileSync(join(dirname(workspace), 'scripts/report.py')), resources.get('scripts/report.py'));
        assert.deepEqual(readFileSync(join(workspace, 'templates/example.txt')), resources.get('templates/example.txt'));
        assert.deepEqual(readFileSync(join(workspace, 'assets/data.bin')), resources.get('assets/data.bin'));
        writeFileSync(join(workspace, 'answer.txt'), 'Done.');
        writeFileSync(trace, '{}');
        return { summary: 'Created output' };
      },
      async assess(_context, files) {
        assert.deepEqual(files.filter((file) => file.role === 'artifact').map((file) => file.locator), ['test/answer.txt']);
        return { verifiable: true, scores: [{ criterion_id: 'correct', score: 1, evidence: 'test/answer.txt' }], reason: 'Verified' };
      },
    },
  }), { processed: 1 });

  const completed = client.execution_get(execution.id);
  assert.equal(completed.recipe_id, base.id);
  assert.equal(completed.result?.published, true);
  const artifact = readTask(client.store, execution.id).artifact;
  assert.ok(artifact);
  const files = readRecipePackage(readFileSync(artifact.zip_path));
  assert.match(Buffer.from(files.get('RECIPE.md')!).toString(), /Create answer.txt from the template/u);
  files.delete('RECIPE.md');
  assert.deepEqual(files, resources);
});
