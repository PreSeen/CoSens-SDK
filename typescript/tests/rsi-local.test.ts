import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { once } from 'node:events';
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import type { AddressInfo } from 'node:net';
import Database from 'better-sqlite3';
import { RSIClient } from '../src/rsi/client.js';
import { RSIClient as PublicRSIClient } from '../src/rsi/index.js';
import { RecipeSchema, type Runtime } from '../src/rsi/models.js';
import { Engine } from '../src/rsi/engine.js';
import { ChatRuntime } from '../src/rsi/runtime.js';
import { bindModelHeaders } from '../src/rsi/model-headers.js';
import { buildRecipePackage } from '../src/rsi/recipe-package.js';
import { seedLegacyRecipe, seedRecipe } from './recipe-fixtures.js';
import {
  legacyRecipes,
  recipeProjection,
  schema,
} from '../src/rsi/store-schema.js';

for (const version of [3, 4, 5, 6]) {
  test(`schema v${version} migration preserves the archived restoration target and the newer candidate`, async () => {
    const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-migrate-'));
    const recipeId = 'recipe_legacy';
    try {
      const db = new Database(join(data_dir, 'rsi-ts.sqlite3'));
      try {
        if (version === 3) {
          db.exec(legacyRecipes);
          db.exec(`
        CREATE TABLE versions (
          id TEXT PRIMARY KEY, recipe_id TEXT REFERENCES recipes(id), rubrics_id TEXT,
          rubrics_metadata TEXT,
          object_id TEXT GENERATED ALWAYS AS (coalesce(recipe_id,rubrics_id)) VIRTUAL,
          number INTEGER NOT NULL, data TEXT NOT NULL, operation_key TEXT UNIQUE,
          UNIQUE(object_id, number)
        );
        INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 8, 'Legacy Recipe',
          NULL, 'version_second', 'version_first', '{"description":"Retained description"}');
        INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 5, 'Waiting Recipe',
          NULL, NULL, NULL, '{"description":"Description without a version"}');
        `);
        } else if (version === 6) {
          db.exec(schema);
          db.exec(`
          INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 'Legacy Recipe', 'recipes/recipe_legacy');
          INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 'Waiting Recipe', 'recipes/recipe_unversioned');
          `);
        } else {
          db.exec(schema.replace(',\n    storage_path TEXT NOT NULL', ''));
          db.exec(`
          INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 'Legacy Recipe');
          INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 'Waiting Recipe');
          `);
        }
        if (version === 4) {
          db.exec(`
          CREATE TABLE recipe_api_metadata (
            recipe_id TEXT PRIMARY KEY REFERENCES recipes(id),
            revision INTEGER NOT NULL, description TEXT NOT NULL
          );
          INSERT INTO recipe_api_metadata VALUES ('recipe_legacy', 8, 'Retired description');
          INSERT INTO recipe_api_metadata VALUES ('recipe_unversioned', 5, 'Retired description');
          CREATE VIEW recipe_records AS SELECT r.*, m.revision, m.description
            FROM recipes r LEFT JOIN recipe_api_metadata m ON m.recipe_id = r.id;
        `);
        }
        if (version >= 5) db.exec(recipeProjection);
        db.pragma(`user_version = ${version}`);
        const insert = db.prepare(
          'INSERT INTO versions (id,recipe_id,number,data) VALUES (?,?,?,?)',
        );
        for (const [id, number] of [
          ['version_first', 1],
          ['version_second', 2],
        ] as const)
          insert.run(
            id,
            recipeId,
            number,
            JSON.stringify({
              id,
              object_id: recipeId,
              number,
              content: {
                title: 'Legacy Recipe',
                applicability: 'Fixture',
                instructions: 'Produce output',
              },
              provenance: {},
              created_at: 1,
            }),
          );
        if (version >= 4)
          db.exec(
            "UPDATE versions SET status=CASE WHEN number=1 THEN 'archived' ELSE 'candidate' END",
          );
      } finally {
        db.close();
      }
      const host = new PublicRSIClient({ data_dir, config: null });
      try {
        const recipe = (await host.recipe_get(recipeId, 'latest'));
        assert.equal(recipe.latest_version_id, 'version_second');
        assert.equal(recipe.latest_qualified_version_id, 'version_first');
        assert.equal(recipe.active_version_id, null);
        assert.equal(recipe.storage_path, `recipes/${recipeId}`);
        for (const id of ['version_first', 'version_second'])
          assert.equal(
            readFileSync(
              join(data_dir, recipe.storage_path, id, 'RECIPE.md'),
              'utf8',
            ),
            '---\nname: "Legacy Recipe"\ndescription: "Fixture"\n---\n\nProduce output\n',
          );
        assert.equal('revision' in recipe, false);
        assert.equal('description' in recipe, false);
        assert.equal(recipe.selected_version?.id, 'version_second');
        assert.deepEqual(recipe.selected_version?.content, {
          name: 'Legacy Recipe',
          description: 'Fixture',
          instructions: 'Produce output',
        });
        const unversioned = (await host.recipe_get('recipe_unversioned', 'latest'));
        assert.equal('revision' in unversioned, false);
        assert.equal('description' in unversioned, false);
        assert.equal(unversioned.selected_version, null);
        (await host.recipe_version_set_archived(recipeId, {
          version_id: 'version_first',
          archived: false,
        }));
        assert.equal(
          (await host.recipe_list())[0].active_version_id,
          'version_first',
        );
        assert.equal(
          (await host.recipe_get(recipeId, 'active')).selected_version?.id,
          'version_first',
        );
        const migrated = new Database(join(data_dir, 'rsi-ts.sqlite3'), {
          readonly: true,
        });
        try {
          assert.deepEqual(
            migrated
              .pragma('table_info(recipes)')
              .map((column) => (column as { name: string }).name)
              .sort(),
            [
              'id',
              'recipe_id',
              'recipe_version_id',
              'recipe_zip_path',
            ],
          );
          assert.deepEqual(
            migrated
              .prepare('SELECT status FROM versions ORDER BY number')
              .pluck()
              .all(),
            ['available', 'candidate'],
          );
          assert.equal(migrated.pragma('user_version', { simple: true }), 10);
          assert.equal(migrated.prepare("SELECT type FROM sqlite_schema WHERE name='versions'").pluck().get(), 'view');
          assert.equal(
            migrated
              .prepare(
                "SELECT name FROM sqlite_schema WHERE name='recipe_api_metadata'",
              )
              .get(),
            undefined,
          );
          assert.deepEqual(migrated.pragma('foreign_key_check'), []);
        } finally {
          migrated.close();
        }
      } finally {
        host.close();
      }
    } finally {
      rmSync(data_dir, { recursive: true, force: true });
    }
  });
}

test('legacy local Recipe import retains validated Skill metadata', async () => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-import-'));
  const client = new PublicRSIClient({ data_dir, config: null });
  try {
    assert.equal('recipe_insert' in client, false);
    const path = join(data_dir, 'SKILL.md');
    const metadata = {
      name: 'order-export',
      description: 'Export orders: "CSV"\nUse when requested.',
    };
    const instructions = '    existing Markdown code block\r\n';
    const generated = { ...metadata, instructions };
    for (const field of ['name', 'description'] as const) {
      const missing: Partial<typeof generated> = { ...generated };
      delete missing[field];
      assert.equal(RecipeSchema.safeParse(missing).success, false);
      const blank = { ...generated, [field]: ' \t ' };
      assert.equal(RecipeSchema.safeParse(blank).success, false);
    }
    for (const [source, error] of [
      ['# Recipe\n\nNo frontmatter', /delimited by ---/],
      ['---\nname: order-export\ndescription: Export orders\n', /delimited by ---/],
      ['---\nname: [broken\ndescription: Export orders\n---', /valid YAML mapping/],
      ['---\nname: first\nname: duplicate\ndescription: Export orders\n---', /valid YAML mapping/],
      ['---\nname: order-export\n---', /name and description/],
      ['---\nname: " "\ndescription: Export orders\n---', /name and description/],
      ['---\nname: order-export\ndescription: 42\n---', /name and description/],
    ] as const) {
      writeFileSync(path, source);
      await assert.rejects(seedRecipe(data_dir, path), error);
    }
    writeFileSync(path, Buffer.from([0x80]));
    await assert.rejects(seedRecipe(data_dir, path), /readable UTF-8 Markdown file/);
    await assert.rejects(seedRecipe(data_dir, data_dir), /readable UTF-8 Markdown file/);
    assert.deepEqual(await client.recipe_list(), []);

    const source = [
      '\uFEFF---',
      'name: order-export',
      'description: |-',
      '  Export orders: "CSV"',
      '  Use when requested.',
      'metadata:',
      '  version: "1.0"',
      '---',
      '',
    ].join('\r\n') + '\r\n' + instructions;
    writeFileSync(path, source);
    const first = await seedRecipe(data_dir, path);
    const second = await seedRecipe(data_dir, path);
    assert.notEqual(first.id, second.id);
    assert.notEqual(first.active_version_id, second.active_version_id);
    assert.deepEqual(await client.recipe_get(first.id, first.active_version_id!), first);
    assert.deepEqual(await client.recipe_get(first.id), first);
    const version = first.selected_version!;
    assert.equal(version.number, 1);
    assert.deepEqual(version.content, generated);
    assert.deepEqual(version.provenance, { source: 'external' });
    assert.equal(first.active_version_id, version.id);
    assert.deepEqual(await client.recipe_get(first.id, 'active'), first);
    assert.deepEqual(await client.execution_list(), []);
    const [row] = await client.recipe_list({ recipe_ids: [first.id] });
    assert.equal('status' in row, false);
    assert.deepEqual(row.execution_ids, []);
    const file = readFileSync(
      join(data_dir, first.storage_path, version.id, 'RECIPE.md'),
      'utf8',
    );
    assert.equal(file, source);

    writeFileSync(path, '---\nname: minimal\ndescription: Metadata only\n---');
    const minimal = await seedRecipe(data_dir, path);
    assert.equal(minimal.selected_version?.content.instructions, '');
  } finally {
    client.close();
    rmSync(data_dir, { recursive: true, force: true });
  }
});

test('schema v7 upgrades while preserving IDs for an unsynchronized Recipe', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-migrate-v7-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const markdown = '---\nname: Report\ndescription: Read a report\n---\nExisting body\n';
  const recipe = seedLegacyRecipe(data_dir, markdown, 7);
  const migrated = new PublicRSIClient({ data_dir, config: null });
  t.after(() => migrated.close());
  assert.deepEqual(await migrated.recipe_get(recipe.id), recipe);
  assert.equal(readFileSync(join(data_dir, recipe.storage_path, recipe.active_version_id!, 'RECIPE.md'), 'utf8'), markdown);
  const proof = new Database(join(data_dir, 'rsi-ts.sqlite3'), { readonly: true });
  try {
    assert.equal(proof.pragma('user_version', { simple: true }), 10);
    assert.equal(proof.prepare('SELECT id FROM recipe_objects').pluck().get(), recipe.id);
    assert.equal(proof.prepare('SELECT COUNT(*) FROM recipes').pluck().get(), 0);
  } finally {
    proof.close();
  }
});

test('schema v8 adopts previously synchronized Recipe and version IDs', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-migrate-v8-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const markdown = '---\nname: Report\ndescription: Read a report\n---\nExisting body\n';
  const recipe = seedLegacyRecipe(data_dir, markdown, 8);
  const localVersion = recipe.active_version_id!;
  const receipt = { recipe_id: 'recipe_synced', recipe_version_id: 'version_synced' };
  const plan = {
    local_recipe_id: recipe.id, local_recipe_version_id: localVersion,
    zip_sha256: 'fixture', idempotency_key: 'old-key', cloud_recipe_id: null,
    base_url: 'https://rsi.example/api', virtual_user_id: null,
  };
  const db = new Database(join(data_dir, 'rsi-ts.sqlite3'));
  try {
    db.exec(`
      ALTER TABLE recipes ADD COLUMN cloud_recipe_id TEXT;
      ALTER TABLE recipes ADD COLUMN cloud_recipe_version_id TEXT;
      ALTER TABLE versions ADD COLUMN cloud_recipe_version_id TEXT;
      PRAGMA user_version=8;
    `);
    db.prepare('UPDATE recipes SET cloud_recipe_id=?,cloud_recipe_version_id=? WHERE id=?')
      .run(receipt.recipe_id, receipt.recipe_version_id, recipe.id);
    db.prepare('UPDATE versions SET cloud_recipe_version_id=? WHERE id=?')
      .run(receipt.recipe_version_id, localVersion);
    db.prepare('INSERT INTO idempotency VALUES (?,?,?,?)')
      .run('recipe_cloud_upload_receipt', localVersion, JSON.stringify(plan), JSON.stringify(receipt));
  } finally {
    db.close();
  }
  const migrated = new PublicRSIClient({ data_dir, config: null });
  t.after(() => migrated.close());
  const canonical = await migrated.recipe_get(recipe.id, localVersion);
  assert.equal(canonical.id, receipt.recipe_id);
  assert.equal(canonical.selected_version?.id, receipt.recipe_version_id);
  assert.equal(readFileSync(join(data_dir, canonical.storage_path, receipt.recipe_version_id, 'RECIPE.md'), 'utf8'), markdown);
  const proof = new Database(join(data_dir, 'rsi-ts.sqlite3'), { readonly: true });
  try {
    assert.equal(proof.pragma('user_version', { simple: true }), 10);
    assert.deepEqual(proof.prepare('SELECT id FROM recipe_objects').pluck().all(), [receipt.recipe_id]);
    assert.equal(proof.prepare('SELECT COUNT(*) FROM recipes').pluck().get(), 0);
    assert.deepEqual(proof.prepare('SELECT id FROM versions').pluck().all(), [receipt.recipe_version_id]);
    assert.equal(proof.prepare('SELECT key FROM idempotency WHERE scope=?').pluck().get('recipe_cloud_upload_receipt'), receipt.recipe_version_id);
    assert.deepEqual(proof.pragma('foreign_key_check'), []);
  } finally {
    proof.close();
  }
});

test('schema v9 backfills one Recipe row per root Task with its approved continuation ZIP', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-migrate-v9-'));
  t.after(() => rmSync(data_dir, { recursive: true, force: true }));
  const recipe = seedLegacyRecipe(data_dir,
    '---\nname: Report\ndescription: Read a report\n---\nExisting body\n', 9);
  const rootId = 'execution_v9_root';
  const continuationId = 'execution_v9_continuation';
  const independentId = 'execution_v9_independent';
  const versionId = recipe.active_version_id!;
  const packages = join(data_dir, 'task-packages');
  const zipPath = join(packages, `${rootId}.zip`);
  const zip = buildRecipePackage(join(data_dir, recipe.storage_path, versionId));
  mkdirSync(packages);
  writeFileSync(zipPath, zip);
  const db = new Database(join(data_dir, 'rsi-ts.sqlite3'));
  try {
    const insert = db.prepare(`
      INSERT INTO executions(
        id,created_at,updated_at,revision,base_execution_id,recipe_id,recipe_version_id,
        status,stage,progress_percent,input_revision,details
      ) VALUES (?,?,?,1,?,?,?, ?,?,?,?,?)
    `);
    const acceptedAnswer = {
      question: 'Which format?', question_revision: 1, selected_option_ids: ['csv'],
      selected_options: [{ id: 'csv', label: 'CSV' }], text: null,
    };
    insert.run(rootId, 1, 2, null, recipe.id, null, 'cancelled', 'cancelled', 20, 1, JSON.stringify({
      questions: [], answers: { 'format:1': { ...acceptedAnswer, continuation_execution_id: continuationId } },
      result: null, error: null,
    }));
    insert.run(continuationId, 2, 4, rootId, recipe.id, versionId, 'completed', 'done', 100, 2, JSON.stringify({
      questions: [], answers: { 'format:1': acceptedAnswer },
      result: { test_status: 'passed', published: true, provisional: false }, error: null,
    }));
    insert.run(independentId, 3, 3, rootId, recipe.id, null, 'queued', 'queued', 0, 1, JSON.stringify({
      questions: [], answers: {}, result: null, error: null,
    }));
    db.prepare('INSERT INTO idempotency VALUES (?,?,?,?)').run(
      'task_recipe_package', rootId, JSON.stringify({ execution_id: continuationId }),
      JSON.stringify({ version_id: versionId, sha256: createHash('sha256').update(zip).digest('hex') }),
    );
  } finally {
    db.close();
  }
  const host = new PublicRSIClient({
    data_dir, model_id: 'fixture', base_url: 'http://127.0.0.1:1/v1', api_key: 'fixture',
    config: { auto_start: false },
  });
  t.after(() => host.close());
  const audit = new Database(join(data_dir, 'rsi-ts.sqlite3'), { readonly: true });
  try {
    assert.deepEqual(audit.prepare('SELECT * FROM recipes ORDER BY id').all(), [
      { id: independentId, recipe_id: recipe.id, recipe_version_id: null, recipe_zip_path: null },
      { id: rootId, recipe_id: recipe.id, recipe_version_id: versionId, recipe_zip_path: `task-packages/${rootId}.zip` },
    ]);
    assert.deepEqual(audit.pragma('foreign_key_check'), []);
    const tasks = await host.task_list();
    assert.deepEqual(tasks.map(({ task_id, execution_id }) => [task_id, execution_id]), [
      [independentId, independentId], [rootId, continuationId],
    ]);
    const task = await host.task_get(rootId);
    assert.deepEqual(task.execution_ids, [rootId, continuationId]);
    assert.deepEqual(task.artifact, { zip_path: zipPath, recipe_id: recipe.id, recipe_version_id: versionId });

    const context_path = join(data_dir, 'new-context.md');
    writeFileSync(context_path, 'New Task');
    const created = await host.execution_create({ evidence: { context_path, files: [] }, idempotency_key: 'new-task' });
    assert.deepEqual(audit.prepare('SELECT * FROM recipes WHERE id=?').get(created.id), {
      id: created.id, recipe_id: created.recipe_id, recipe_version_id: null, recipe_zip_path: null,
    });
  } finally {
    audit.close();
  }
});

test('recover leaves pending work for the Client with the matching model key', async () => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-header-bind-'));
  const options = { data_dir, model_id: 'fixture', base_url: 'http://localhost:9/v1', config: { auto_start: false } };
  const client = new PublicRSIClient({ ...options, api_key: 'a' });
  const other = new PublicRSIClient({ ...options, api_key: 'b' });
  const readOnly = new PublicRSIClient({ data_dir, config: null });
  try {
    const path = join(data_dir, 'context.md');
    writeFileSync(path, 'Task transcript');
    const execution = await client.execution_create({
      evidence: { context_path: path, files: [] }, idempotency_key: 'example',
    });
    assert.deepEqual(await other.recover(), { started: 0 });
    assert.deepEqual(
      await other.recover({ recipe_id: execution.recipe_id! }),
      { started: 0 },
    );
    assert.deepEqual(await readOnly.recover(), { started: 0 });
    assert.equal((await client.execution_get(execution.id)).status, 'queued');
  } finally {
    client.close();
    other.close();
    readOnly.close();
    rmSync(data_dir, { recursive: true, force: true });
  }
});

test('independent Node hooks enqueue work that persists and passes a file test after the hooks exit', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-ts-'));
  const data_dir = join(directory, 'rsi');
  let active = 0;
  let peak = 0;
  let requests = 0;
  let clarificationToken = 'Bearer fixture-clarification-old';
  let rejectedClarificationRequests = 0;
  let retryExecutions = 0;
  let retryGenerations = 0;
  const server = createServer(async (req, res) => {
    active++;
    peak = Math.max(peak, active);
    requests++;
    try {
      assert.equal(req.url, '/v1/chat/completions');
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const input = JSON.parse(Buffer.concat(chunks).toString());
      const clarification = input.model === 'fixture-clarification-model';
      if (clarification) {
        if (req.headers.authorization !== clarificationToken) {
          rejectedClarificationRequests++;
          res.writeHead(401).end();
          return;
        }
        assert.equal(input.max_tokens, 4096);
      }
      const key = input.model.slice(-1);
      if (['a', 'b', 'c'].includes(key)) {
        assert.equal(req.headers.authorization, 'Bearer fixture-model-' + key);
        assert.equal(req.headers['x-auth-type'], undefined);
        assert.equal(req.headers['x-api-key'], undefined);
      }
      const { context, evidence_catalog } = JSON.parse(
        input.messages.find((m: { role: string }) => m.role === 'user').content,
      );
      const afterTool = input.messages.some(
        (m: { role: string }) => m.role === 'tool',
      );
      const tools = new Set(
        input.tools?.map(
          (tool: { function: { name: string } }) => tool.function.name,
        ),
      );
      const token = req.headers.authorization?.replace(/^Bearer /, '');
      if (input.model === 'fixture-retry-model') {
        assert.equal(input.max_tokens, 4096);
        if (tools.has('RecipeContent') && token === 'timeout-b') retryGenerations++;
        if (tools.has('write_output') && !afterTool) {
          if (token === 'timeout-a') {
            // Keep the connection open until the SDK's actual request deadline.
            await new Promise<void>((resolve) => res.once('close', resolve));
            return;
          }
          assert.equal(token, 'timeout-b');
          retryExecutions++;
        }
      }
      if (token === 'fixture-expired' && tools.has('write_output')) {
        res.writeHead(401).end();
        return;
      }
      if (token === 'fixture-refreshed') {
        assert.equal(input.model, 'fixture-refreshed-model');
        assert.equal(input.max_tokens, 16384);
      }
      if (tools.has('web_search')) assert.ok(tools.has('RecipeContent'));
      let call: { name: string; args: object } | undefined;
      const evidence = evidence_catalog[0];
      const attribution = evidence && {
        possible_causes: [
          {
            observed_issue: 'Prior output used 24; user corrected it to 42',
            reason: 'The digits may have been transposed',
            uncertainty: 'The previous reasoning was not recorded',
            evidence_refs: [
              { material_id: evidence.material_id, locator: evidence.locator },
            ],
          },
        ],
      };
      const feedbackSource = evidence_catalog.find(
        (entry: { role: string }) => entry.role === 'feedback',
      );
      const feedback =
        evidence && feedbackSource
          ? [
              {
                kind: 'correction',
                rule: 'Write 42 as the answer',
                evidence_refs: [
                  {
                    material_id: evidence.material_id,
                    locator: evidence.locator,
                  },
                ],
              },
              {
                kind: 'requirement',
                rule: 'The file contains only the answer',
                evidence_refs: [
                  {
                    material_id: feedbackSource.material_id,
                    locator: feedbackSource.locator,
                  },
                ],
              },
            ]
          : [];
      const longAnalysis = tools.has('Analysis') && evidence.size > 4 * 1024 * 1024;
      const analysisPages = longAnalysis ? input.messages
        .filter((message: { role: string }) => message.role === 'tool')
        .map((message: { content: string }) => JSON.parse(message.content)) : [];
      if (longAnalysis && analysisPages.length < 3) {
        const args = { locator: evidence.locator, limit: 512, offset: 0, query: undefined as string | undefined };
        if (analysisPages.length === 1) args.offset = analysisPages[0].next_offset;
        if (analysisPages.length === 2) args.query = 'TAIL_FINAL_RULE';
        call = { name: 'read_evidence', args };
      } else if (tools.has('FeedbackExtraction') && afterTool) {
        assert.deepEqual(
          evidence_catalog.map((entry: { role: string }) => entry.role),
          ['context', 'feedback'],
        );
        const reads = input.messages.filter(
          (m: { role: string }) => m.role === 'tool',
        );
        call =
          reads.length === 1
            ? {
                name: 'read_evidence',
                args: { locator: feedbackSource.locator },
              }
            : { name: 'FeedbackExtraction', args: { items: feedback } };
      } else if (
        tools.has('write_output') &&
        context.test_task.instruction.startsWith('Repeat the current task')
      ) {
        assert.deepEqual(
          evidence_catalog.map((entry: { role: string }) => entry.role),
          ['input'],
        );
        const read = input.messages.find(
          (m: { role: string }) => m.role === 'tool',
        );
        const written =
          input.messages.filter((m: { role: string }) => m.role === 'tool')
            .length > 1;
        if (!read)
          call = { name: 'read_evidence', args: { locator: evidence.locator } };
        else if (!written) {
          assert.match(read.content, /CURRENT_CASE_42/);
          call = {
            name: 'write_output',
            args: { path: 'answer.txt', content: '42' },
          };
        }
      } else if (!afterTool) {
        call = tools.has('write_output')
          ? {
              name: 'write_output',
              args: { path: 'answer.txt', content: '42' },
            }
          : { name: 'read_evidence', args: { locator: evidence.locator } };
      } else if (tools.has('Analysis')) {
        if (longAnalysis) {
          assert.equal(analysisPages[0].text_omitted, true);
          assert.equal(analysisPages[1].offset, analysisPages[0].next_offset);
          assert.ok(analysisPages[2].match_offset > 4 * 1024 * 1024);
          assert.match(analysisPages[2].text, /TAIL_FINAL_RULE/);
          assert.equal(analysisPages[2].end_offset, analysisPages[2].total_chars);
        }
        assert.ok(
          evidence_catalog.some(
            (entry: { role: string }) => entry.role === 'reference',
          ),
        );
        assert.deepEqual(context.user_feedback, feedback);
        const needsAnswer = clarification && Object.keys(context.answers).length === 0;
        call = {
          name: 'Analysis',
          args: {
            recipe_name: 'Write checked answers',
            sufficient_information: !needsAnswer,
            hypothesis: needsAnswer ? 'Use plain text until confirmed' : null,
            questions: needsAnswer ? [{
              id: 'format',
              question: 'Which output format should be used?',
              assumption: 'Use plain text',
              options: [{ id: 'plain', label: 'Plain text' }, { id: 'json', label: 'JSON' }],
            }] : [],
            attribution,
            rubrics: {
              criteria: [
                {
                  id: 'answer',
                  description: 'Write only the answer 42',
                  source: 'user_feedback',
                  evidence: feedback.flatMap((item) => item.evidence_refs),
                },
                {
                  id: 'negative-answer-format',
                  description:
                    'If the answer is negative, preserve the minus sign',
                  source: 'agent_inference',
                  required: true,
                },
              ],
            },
          },
        };
      } else if (
        tools.has('web_search') &&
        !input.messages.some(
          (m: { role: string; content: string }) =>
            m.role === 'tool' &&
            m.content.includes('https://docs.example.org/format'),
        )
      ) {
        call = {
          name: 'web_search',
          args: { query: 'public text formatting conventions' },
        };
      } else if (tools.has('RecipeContent')) {
        assert.deepEqual(context.analysis.attribution, attribution);
        const corrected = input.messages.some(
          (m: { role: string; content: string }) =>
            m.role === 'tool' && m.content.includes('matching its schema'),
        );
        call = {
          name: 'RecipeContent',
          args: {
            name: 'Write answer',
            ...(corrected ? { description: 'Text output' } : {}),
            instructions: 'Write 42 to answer.txt',
          },
        };
      } else if (tools.has('Assessment')) {
        if (context.test_task.replay_current_case) {
          assert.deepEqual(context.objective_checks, []);
          assert.deepEqual(
            evidence_catalog.map((entry: { role: string }) => entry.role),
            ['artifact', 'input', 'trace'],
          );
        } else {
          assert.ok(context.objective_checks[0].passed);
        }
        assert.equal(context.user_feedback, undefined);
        assert.equal(context.rubrics.criteria[0].evidence, undefined);
        assert.ok(
          evidence_catalog.some(
            (entry: { role: string }) => entry.role === 'trace',
          ),
        );
        call = {
          name: 'Assessment',
          args: {
            verifiable: true,
            scores: [
              { criterion_id: 'answer', score: 1, evidence: 'test/answer.txt' },
              {
                criterion_id: 'negative-answer-format',
                status: 'unverified',
                score: null,
                evidence:
                  'The answer 42 is positive, so the condition was not exercised',
              },
            ],
            reason: 'Output verified',
          },
        };
      }
      await new Promise((resolve) => setTimeout(resolve, 25));
      res.setHeader('Content-Type', 'application/json');
      res.end(
        JSON.stringify({
          id: 'fixture_' + requests,
          object: 'chat.completion',
          model: input.model,
          created: 1,
          choices: [
            {
              index: 0,
              finish_reason: call ? 'tool_calls' : 'stop',
              message: {
                role: 'assistant',
                content: call ? null : 'Done',
                ...(call && {
                  tool_calls: [
                    {
                      id: 'call_' + requests,
                      type: 'function',
                      function: {
                        name: call.name,
                        arguments: JSON.stringify(call.args),
                      },
                    },
                  ],
                }),
              },
            },
          ],
          usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
        }),
      );
    } catch (error) {
      console.error(error);
      res
        .writeHead(400)
        .end(JSON.stringify({ error: { message: 'Invalid fixture request' } }));
    } finally {
      active--;
    }
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const port = (server.address() as AddressInfo).port;
  const client = new RSIClient({
    data_dir,
    config: {
      model: { model: 'fixture', base_url: `http://127.0.0.1:${port}/v1` },
      max_executions: 2,
      max_model_requests: 1,
    },
    auto_start: false,
  });
  try {
    const path = join(directory, 'context.md');
    writeFileSync(
      path,
      'Produce answer.txt with 42. The user corrected the prior output from 24 to 42.',
    );
    const longPath = join(directory, 'long-context.jsonl');
    writeFileSync(longPath, 'session trace\n'.repeat(330_000) + 'TAIL_FINAL_RULE: Write only 42.\n');
    const feedbackPath = join(directory, 'feedback.md');
    writeFileSync(feedbackPath, 'User: Put only the answer in the file.');
    const currentPath = join(directory, 'current.txt');
    writeFileSync(currentPath, 'CURRENT_CASE_42');
    const referencePath = join(directory, 'reference.md');
    writeFileSync(
      referencePath,
      'Execution trace: the file was written with the answer.',
    );
    const material = client.material_create({
      manifest: {
        context_path: path,
        files: [
          { path: feedbackPath, role: 'feedback' },
          { path: referencePath, role: 'reference' },
        ],
      },
    });
    const script = `
      import assert from 'node:assert/strict';
      import { RSIClient } from 'cosens-sdk/rsi';
      const [data_dir, evidenceText, key, configText] = process.argv.slice(1);
      const { model, base_url } = JSON.parse(configText).model;
      const client = new RSIClient({
        data_dir, model_id: model + '-' + key, base_url, api_key: 'fixture-model-' + key,
        config: { headers: {
          Authorization: 'Bearer fixture-upload-token', 'X-Auth-Type': 'access_token', 'X-Api-Key': 'fixture-upload-key',
        } },
      });
      const execution = await client.execution_create({
        evidence: JSON.parse(evidenceText), idempotency_key: key,
      });
      assert.equal('material_create' in client, false);
      assert.equal('run_pending' in client, false);
      assert.equal('wait' in client, false);
      assert.equal('material_ids' in execution, false);
      assert.equal(execution.evidence.length, 4);
      assert.match(execution.recipe_id, /^recipe_/);
      client.close();
      process.stdout.write(JSON.stringify({id:execution.id,recipe_id:execution.recipe_id}));
    `;
    const runHook = async (source: string, args: string[], executable = process.execPath) => {
      const child = spawn(
        executable,
        executable === process.execPath
          ? ['--input-type=module', '-e', source, ...args]
          : ['-e', source, ...args],
        {
          cwd: new URL('..', import.meta.url),
          env: {
            ...process.env,
            RSI_MODEL_API_KEY: 'fixture',
            COSENS_RSI_WEB_SEARCH_API_KEY: '',
          },
        },
      );
      let stdout = '',
        stderr = '';
      child.stdout.setEncoding('utf8').on('data', (chunk) => {
        stdout += chunk;
      });
      child.stderr.setEncoding('utf8').on('data', (chunk) => {
        stderr += chunk;
      });
      const [code] = await once(child, 'close');
      assert.equal(code, 0, stderr);
      return stdout;
    };
    const hookResults = await Promise.all(
      ['a', 'b', 'c', 'a'].map((key) =>
        runHook(script, [
          data_dir,
          JSON.stringify({
            context_path: key === 'c' ? longPath : path,
            files: [
              { path: feedbackPath, role: 'feedback' },
              { path: referencePath, role: 'reference' },
              { path: currentPath, role: 'input' },
            ],
          }),
          key,
          JSON.stringify(client.config),
        ]),
      ),
    );
    const created = hookResults.map(
      (value) => JSON.parse(value) as { id: string; recipe_id: string },
    );
    const ids = created.map((item) => item.id);
    assert.deepEqual(created[0], created[3]);
    assert.equal(readdirSync(join(data_dir, 'materials')).length, 4);

    for (const id of new Set(ids)) {
      const run = await client.wait(id, { timeout: 25, poll_interval: 0.05 });
      const captured = client.material_get(run.material_ids[0]);
      assert.equal(
        run.runtime_config.model.model,
        'fixture-' + ['a', 'b', 'c'][ids.indexOf(id)],
      );
      assert.doesNotMatch(JSON.stringify(run.runtime_config), /fixture-(?:gateway|provider)/);
      assert.equal(
        JSON.parse(readFileSync(run.test_results[0].trace_path!, 'utf8')).model,
        run.runtime_config.model.model,
      );
      assert.equal(run.status, 'completed', JSON.stringify(run.error));
      assert.equal(run.recipe_id, created[ids.indexOf(id)].recipe_id);
      assert.equal(run.result?.test_status, 'passed');
      assert.equal(run.test_results.length, 1);
      assert.equal(run.test_results[0].score, 1);
      assert.deepEqual(
        run.test_results[0].scores.map(({ status, score }) => ({
          status,
          score,
        })),
        [
          { status: 'verified', score: 1 },
          { status: 'unverified', score: null },
        ],
      );
      assert.deepEqual(
        run.analysis?.attribution?.possible_causes[0].evidence_refs,
        [{ material_id: captured.id, locator: captured.files[0].locator }],
      );
      assert.deepEqual(
        run.analysis?.feedback?.map((item) => item.evidence_refs[0].locator),
        [captured.files[0].locator, captured.files[1].locator],
      );
      assert.equal(run.result?.published, true);
      const taskPackage = join(data_dir, 'task-packages', `${run.id}.zip`);
      assert.ok(readFileSync(taskPackage).length > 0);
      assert.equal(
        readFileSync(run.test_results[0].artifacts[0].path, 'utf8'),
        '42',
      );
      assert.equal(
        client.recipe_get(run.recipe_id!).selected_version?.id,
        run.recipe_version_id,
      );
      const host = new PublicRSIClient({ data_dir, config: null });
      try {
        const task = await host.task_get(run.id);
        assert.deepEqual(task.artifact, {
          zip_path: taskPackage, recipe_id: run.recipe_id, recipe_version_id: run.recipe_version_id,
        });
        await assert.rejects(host.task_restart(run.id, { idempotency_key: 'already-approved' }), /approved Task/);
        const available = (await host.recipe_list())
          .find((row) => row.recipe_id === run.recipe_id);
        assert.equal('recipe_activate' in host, false);
        const detail = (await host.recipe_get(run.recipe_id!, 'latest'));
        const allRecipes = (await host.recipe_list());
        const requested = allRecipes.slice(0, 2);
        assert.equal(requested.length, 2);
        const selectedIds = requested.map((row) => row.recipe_id!);
        assert.deepEqual(
          (
            await host.recipe_list({ recipe_ids: [...selectedIds].reverse() })
          ).map((row) => row.recipe_id),
          selectedIds,
        );
        assert.deepEqual(available, {
          recipe_id: run.recipe_id,
          execution_ids: [run.id],
          name: run.name,
          active_version_id: run.recipe_version_id,
          created_at: detail.created_at,
          updated_at: detail.updated_at,
        });
        const version_id = run.recipe_version_id!;
        (await host.recipe_version_set_archived(run.recipe_id!, {
          version_id,
          archived: true,
        }));
        const archived = (await host.recipe_list())
          .find((row) => row.recipe_id === run.recipe_id);
        assert.equal(archived?.active_version_id, null);
        assert.equal(
          (await host.recipe_get(run.recipe_id!, version_id)).selected_version
            ?.archived,
          true,
        );
        (await host.recipe_version_set_archived(run.recipe_id!, {
          version_id,
          archived: false,
        }));
        assert.equal(
          (await host.recipe_get(run.recipe_id!, 'active')).selected_version?.id,
          version_id,
        );
        assert.equal(
          (await host.recipe_get(
            available!.recipe_id!,
            available!.active_version_id!,
          )).selected_version?.id,
          run.recipe_version_id,
        );
      } finally {
        host.close();
      }
      const checkpoint = new Database(
        join(data_dir, 'executions', run.id, 'checkpoints-1.sqlite3'),
        { readonly: true, fileMustExist: true },
      );
      try {
        const namespaces = checkpoint
          .prepare('SELECT DISTINCT checkpoint_ns FROM checkpoints')
          .pluck()
          .all() as string[];
        assert.ok(namespaces.includes(''));
        assert.ok(
          !namespaces.some((namespace) => namespace.startsWith('test_recipe:')),
        );
      } finally {
        checkpoint.close();
      }
    }
    assert.equal(peak, 2);
    assert.equal(requests, 41); // Includes two more pages for the long context.
    const db = new Database(join(data_dir, 'rsi-ts.sqlite3'), {
      readonly: true,
    });
    try {
      assert.deepEqual(
        db
          .prepare(
            `SELECT 'execution' AS kind, count(*) AS n FROM executions
             UNION ALL SELECT 'material', count(*) FROM evidence_snapshots
             UNION ALL SELECT 'recipe', count(*) FROM recipes
             UNION ALL SELECT 'rubrics', count(DISTINCT rubrics_id) FROM versions WHERE rubrics_id IS NOT NULL`,
          )
          .all(),
        [
          { kind: 'execution', n: 3 },
          { kind: 'material', n: 4 },
          { kind: 'recipe', n: 3 },
          { kind: 'rubrics', n: 3 },
        ],
      );
    } finally {
      db.close();
    }

    const recovery = new RSIClient({
      data_dir: join(directory, 'recover-once'),
      config: { ...client.config, max_executions: 1 },
      auto_start: false,
    });
    try {
      const source = recovery.material_create({ manifest: material.manifest });
      const queueForRecovery = async (confirmation_ref: string) => {
        const run = await recovery.execution_create({
          material_ids: [source.id],
          policy: {
            artifact_confirmed: true,
            confirmation_ref,
            test_task: {
              instruction: 'Create answer.txt',
              checks: [{ kind: 'text_equals', path: 'answer.txt', value: '42' }],
            },
          },
        });
        return recovery.store.tx(() =>
          recovery.store.save({ ...run, recipe_id: recovery.store.createRecipe().id }),
        );
      };
      const other = await queueForRecovery('other-recipe');
      const execution = await queueForRecovery('selected-recipe');
      const recoverFromHook = async (recipeId?: string) => JSON.parse(
        await runHook(
          `
          import { RSIClient } from 'cosens-sdk/rsi';
          const client = new RSIClient({ data_dir: process.argv[1], config: null });
          const recipe_id = process.argv[2];
          console.log(JSON.stringify(await (recipe_id ? client.recover({ recipe_id }) : client.recover())));
          client.close();
          `,
          recipeId === undefined ? [recovery.data_dir] : [recovery.data_dir, recipeId],
        ),
      );
      assert.deepEqual(await recoverFromHook('recipe_missing'), { started: 0 });
      assert.ok(recovery.store.reserve('worker', 'stopped-worker', 1, 3));
      assert.deepEqual(
        recovery.store.claim('stopped-worker', 3, 1, undefined, execution.recipe_id!),
        { id: execution.id, fence: 1 },
      );
      assert.deepEqual(
        await recoverFromHook(execution.recipe_id!),
        { started: 0, scheduled: true },
      );

      const run = await recovery.wait(execution.id, { timeout: 20 });
      assert.equal(run.status, 'completed', JSON.stringify(run.error));
      assert.equal(run.result?.test_status, 'passed');
      assert.ok(run.test_results[0].trace_path?.includes('lease-2'));
      const workerSlots = recovery.store.db.prepare("SELECT 1 FROM slots WHERE kind='worker'");
      const deadline = Date.now() + 5_000;
      while (workerSlots.get() && Date.now() < deadline)
        await new Promise((resolve) => setTimeout(resolve, 50));
      assert.equal(workerSlots.get(), undefined);
      assert.equal(recovery.execution_get(other.id).status, 'queued');
      assert.deepEqual(await recoverFromHook(execution.recipe_id!), { started: 0 });

      assert.deepEqual(await recoverFromHook(), { started: 1 });
      const remaining = await recovery.wait(other.id, { timeout: 20 });
      assert.equal(remaining.status, 'completed', JSON.stringify(remaining.error));
    } finally {
      for (const run of (await recovery.execution_list())) {
        if (run.status === 'running' || run.status === 'queued')
          (await recovery.execution_cancel(run.id));
      }
      recovery.close();
    }

    await t.test('refreshed model credentials resume the same checkpoint and isolate accounts', async () => {
      const { RSIClient: BuiltClient } = await import('cosens-sdk/rsi');
      const data_dir = join(directory, 'refresh-token');
      const expiredOptions = {
        data_dir, model_id: 'fixture-expired-model', base_url: client.config.model.base_url!, api_key: 'fixture-expired',
        config: { auto_start: false },
      };
      const expiredHeaders = { Authorization: `Bearer ${expiredOptions.api_key}` };
      const freshOptions = {
        ...expiredOptions,
        model_id: 'fixture-refreshed-model',
        api_key: 'fixture-refreshed',
        config: { ...expiredOptions.config, max_tokens: 16384, timeout: 600 },
      };
      const expired = new BuiltClient({ ...expiredOptions, config: { ...expiredOptions.config, credential_scope: 'account-a' } });
      const refreshed = new BuiltClient({ ...freshOptions, config: { ...freshOptions.config, credential_scope: 'account-a' } });
      const other = new BuiltClient({ ...freshOptions, config: { ...freshOptions.config, credential_scope: 'account-b' } });
      const legacy = new BuiltClient(expiredOptions);
      const reader = new RSIClient({ data_dir, auto_start: false });
      try {
        const evidence = {
          context_path: path,
          files: [
            { path: feedbackPath, role: 'feedback' as const },
            { path: referencePath, role: 'reference' as const },
            { path: currentPath, role: 'input' as const },
          ],
        };
        const unrelated = await other.execution_create({ evidence, idempotency_key: 'other-account' });
        const old = await expired.execution_create({ evidence, idempotency_key: 'refresh-token' });
        const beforeScope = await legacy.execution_create({ evidence, idempotency_key: 'legacy-binding' });
        await expired.recover({ recipe_id: old.recipe_id! });
        const failed = await reader.wait(old.id, { timeout: 20 });
        assert.equal(failed.error?.upstream_status, 401);
        assert.ok(failed.recipe_version_id);
        assert.deepEqual(await expired.recover({ recipe_id: old.recipe_id! }), { started: 0 });
        assert.deepEqual(await other.recover({ recipe_id: old.recipe_id!, previous_headers: expiredHeaders }), { started: 0 });

        await refreshed.recover();
        const completed = await reader.wait(old.id, { timeout: 20 });
        assert.equal(completed.status, 'completed', JSON.stringify(completed.error));
        assert.equal(completed.result?.test_status, 'passed');
        assert.equal(completed.recipe_version_id, failed.recipe_version_id);
        assert.equal(completed.runtime_config.model.model, freshOptions.model_id);
        assert.equal(completed.runtime_config.model.max_tokens, freshOptions.config.max_tokens);
        assert.equal(completed.runtime_config.model.timeout, freshOptions.config.timeout);
        assert.equal(reader.execution_get(unrelated.id).status, 'queued');
        assert.equal(reader.execution_get(beforeScope.id).status, 'queued');

        await refreshed.recover({ recipe_id: beforeScope.recipe_id!, previous_headers: expiredHeaders });
        const migrated = await reader.wait(beforeScope.id, { timeout: 20 });
        assert.equal(migrated.status, 'completed', JSON.stringify(migrated.error));
        assert.equal(migrated.runtime_config.model.credential_scope, completed.runtime_config.model.credential_scope);
        assert.equal(reader.execution_get(unrelated.id).status, 'queued');
      } finally {
        for (const host of [expired, refreshed, other, legacy, reader]) host.close();
      }
    });

    await t.test('answering with a new token starts the clarification continuation automatically', async () => {
      const dataDir = join(directory, 'refresh-clarification');
      const reader = new RSIClient({ data_dir: dataDir, auto_start: false });
      const options = {
        model_id: 'fixture-clarification-model',
        base_url: client.config.model.base_url!,
        config: { credential_scope: 'fixture-account', max_tokens: 4096 },
      };
      const callHook = (request: object) => runHook(
        `
        import { RSIClient } from 'cosens-sdk/rsi';
        const [data_dir, payload] = process.argv.slice(-2);
        const { options, create, answer } = JSON.parse(payload);
        const client = new RSIClient({ data_dir, ...options });
        const result = create
          ? await client.execution_create(create)
          : await client.execution_clarification_answer(answer.execution_id, answer.value);
        console.log(JSON.stringify(result));
        client.close();
        `,
        [dataDir, JSON.stringify(request)],
        process.env.RSI_TEST_BUN_EXECUTABLE,
      ).then(JSON.parse);
      try {
        const first = await callHook({
          options: { ...options, api_key: clarificationToken.slice('Bearer '.length) },
          create: {
            idempotency_key: 'clarification-token-rotation',
            evidence: {
              context_path: path,
              files: [
                { path: feedbackPath, role: 'feedback' },
                { path: referencePath, role: 'reference' },
                { path: currentPath, role: 'input' },
              ],
            },
          },
        });
        const parent = await reader.wait(first.id, { timeout: 20 });
        assert.equal(parent.status, 'completed', JSON.stringify(parent.error));
        assert.equal(parent.result?.provisional, true);
        assert.equal(parent.questions.length, 1);
        const question = parent.questions[0];
        clarificationToken = 'Bearer fixture-clarification-new';

        const request = {
          options: {
            ...options, base_url: options.base_url + '/', api_key: clarificationToken.slice('Bearer '.length),
            config: { ...options.config, max_tokens: 8192 },
          },
          answer: {
            execution_id: parent.id,
            value: { question_id: question.id, question_revision: question.revision, answer: ['plain'] },
          },
        };
        const submitted = await callHook(request);
        assert.ok(submitted.continuation_execution_id);
        const child = await reader.wait(submitted.continuation_execution_id, { timeout: 10, poll_interval: 0.05 });
        assert.equal(child.status, 'completed', JSON.stringify(child.error));
        assert.equal(child.result?.published, true);
        assert.equal(child.recipe_id, parent.recipe_id);
        assert.equal(child.runtime_config.model.base_url, options.base_url);
        assert.equal(child.runtime_config.model.max_tokens, options.config.max_tokens);
        assert.equal(rejectedClarificationRequests, 0);
        assert.deepEqual(await callHook(request), submitted);
        assert.equal(reader.store.db.prepare('SELECT COUNT(*) FROM recipes').pluck().get(), 1);
        assert.equal(reader.execution_list_all().length, 2);
      } finally {
        for (const execution of reader.execution_list_all()) {
          if (execution.status === 'queued' || execution.status === 'running') {
            reader.execution_cancel(execution.id);
          }
        }
        reader.close();
      }
    });

    await t.test('recover with a retry key resumes a timed-out test on the same execution and candidate', async () => {
      const { RSIClient: BuiltClient } = await import('cosens-sdk/rsi');
      const data_dir = join(directory, 'retry-timeout');
      const settings = {
        data_dir,
        model_id: 'fixture-retry-model',
        base_url: client.config.model.base_url!,
        config: { credential_scope: 'retry-account', timeout: 1, max_tokens: 4096 },
      };
      const original = new BuiltClient({ ...settings, api_key: 'timeout-a' });
      const freshSettings = {
        ...settings,
        base_url: settings.base_url + '/',
        api_key: 'timeout-b',
        config: { ...settings.config, max_tokens: 8192, auto_start: false },
      };
      const refreshed = new BuiltClient(freshSettings);
      const other = new BuiltClient({
        ...freshSettings, config: { ...freshSettings.config, credential_scope: 'other-account' },
      });
      const wrongRoute = new BuiltClient({ ...freshSettings, model_id: 'wrong-model' });
      const reader = new RSIClient({ data_dir, auto_start: false });
      try {
        const request = {
          idempotency_key: 'retry-test-original',
          evidence: {
            context_path: path,
            files: [
              { path: feedbackPath, role: 'feedback' as const },
              { path: referencePath, role: 'reference' as const },
              { path: currentPath, role: 'input' as const },
            ],
          },
        };
        const created = await original.execution_create(request);
        const failed = await reader.wait(created.id, { timeout: 15, poll_interval: 0.05 });
        assert.equal(failed.error?.code, 'timeout');
        assert.ok(failed.recipe_id && failed.recipe_version_id);
        const id = failed.recipe_id;
        const retry = { recipe_id: id, retry_key: 'retry-click' };
        assert.deepEqual(await refreshed.recover({ recipe_id: id }), { started: 0 });
        assert.equal((await original.execution_create(request)).status, 'failed');
        assert.equal('recipe_retry' in refreshed, false);
        await assert.rejects(
          refreshed.recover({ retry_key: retry.retry_key }),
          { code: 'invalid_argument' },
        );
        await assert.rejects(
          refreshed.recover({ ...retry, previous_headers: {} }),
          { code: 'invalid_argument' },
        );
        await assert.rejects(other.recover(retry), { code: 'invalid_state' });
        await assert.rejects(wrongRoute.recover(retry), { code: 'invalid_state' });

        const executable = process.env.RSI_NODE_EXECUTABLE;
        process.env.RSI_NODE_EXECUTABLE = join(data_dir, 'missing-worker-runtime');
        const brokenLauncher = new BuiltClient(freshSettings);
        if (executable === undefined) delete process.env.RSI_NODE_EXECUTABLE;
        else process.env.RSI_NODE_EXECUTABLE = executable;
        try {
          await assert.rejects(brokenLauncher.recover(retry), { code: 'ENOENT' });
          assert.equal(reader.execution_get(failed.id).status, 'queued');
        } finally {
          brokenLauncher.close();
        }
        await assert.rejects(
          refreshed.recover({ ...retry, retry_key: 'new-click-while-queued' }),
          { code: 'invalid_state' },
        );
        const receipt = { started: 0, recipe_id: id, execution_id: failed.id };
        const replies = await Promise.all([
          refreshed.recover(retry),
          refreshed.recover(retry),
        ]);
        assert.equal(replies.filter((reply) => reply.scheduled).length, 1);
        for (const { scheduled: _, ...reply } of replies) assert.deepEqual(reply, receipt);
        const completed = await reader.wait(failed.id, { timeout: 15, poll_interval: 0.05 });
        assert.equal(completed.status, 'completed', JSON.stringify(completed.error));
        assert.equal(completed.result?.published, true);
        assert.equal(completed.recipe_version_id, failed.recipe_version_id);
        assert.equal(completed.input_revision, failed.input_revision);
        assert.deepEqual(completed.material_ids, failed.material_ids);
        assert.equal(completed.runtime_config.model.base_url, settings.base_url);
        assert.equal(retryExecutions, 1);
        assert.equal(retryGenerations, 0);
        assert.equal(reader.execution_list_all().length, 1);
        assert.deepEqual(await refreshed.recover(retry), receipt);
        await assert.rejects(
          refreshed.recover({ ...retry, retry_key: 'new-click-after-success' }),
          { code: 'invalid_state' },
        );
      } finally {
        for (const host of [reader, original, refreshed, other, wrongRoute]) host.close();
      }
    });

    const environment = {
      RSI_MODEL_API_KEY: 'fixture',
      COSENS_RSI_WEB_SEARCH_API_KEY: 'fixture-search',
      COSENS_RSI_WEB_SEARCH_ALLOWED_DOMAINS: 'example.org',
      COSENS_RSI_WEB_SEARCH_BLOCKED_DOMAINS: 'blocked.example.org',
    };
    const saved = Object.keys(environment).map(
      (key) => [key, process.env[key]] as const,
    );
    Object.assign(process.env, environment);
    const fetch = globalThis.fetch;
    const search = t.mock.method(globalThis, 'fetch', (input, init) => {
      if (String(input) !== 'https://api.exa.ai/search')
        return fetch(input, init);
      assert.equal(
        new Headers(init?.headers).get('x-api-key'),
        'fixture-search',
      );
      assert.deepEqual(JSON.parse(String(init?.body)), {
        query: 'public text formatting conventions',
        type: 'auto',
        numResults: 5,
        contents: { highlights: true },
        includeDomains: ['example.org'],
        excludeDomains: ['blocked.example.org'],
      });
      return Promise.resolve(
        Response.json({
          results: [
            {
              url: 'https://docs.example.org/format',
              title: 'Formatting',
              highlights: ['Public reference text'],
            },
            {
              url: 'https://blocked.example.org/item',
              highlights: ['Blocked reference'],
            },
          ],
        }),
      );
    });
    try {
      const tracePath = join(directory, 'search-trace.json');
      const runtime = new ChatRuntime(client, 'search-test', () => {});
      const run = client.execution_get(ids[0]);
      await runtime.generate(
        { analysis: run.analysis },
        client.material_get(run.material_ids[0]).files,
        tracePath,
      );
      const trace = readFileSync(tracePath, 'utf8');
      assert.equal(
        search.mock.calls.filter(
          ({ arguments: [url] }) => String(url) === 'https://api.exa.ai/search',
        ).length,
        1,
      );
      assert.ok(trace.includes('Public reference text'));
      assert.ok(!trace.includes('Blocked reference'));
      assert.ok(!trace.includes('fixture-search'));
    } finally {
      search.mock.restore();
      for (const [key, value] of saved) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  } finally {
    for (const run of client.execution_list()) {
      if (run.status === 'running' || run.status === 'queued')
        client.execution_cancel(run.id);
    }
    client.close();
    server.close();
    // Detached workers have an idle grace period before closing their SQLite handles.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    rmSync(directory, { recursive: true, force: true });
  }
});

test('all clarification answers create one continuation and only the corrected version is tested', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-revision-'));
  const client = new RSIClient({
    data_dir: join(directory, 'rsi'),
    config: { model: { model: 'fixture' } },
    auto_start: false,
  });
  const path = join(directory, 'context.md');
  writeFileSync(path, 'Write a number using the user-selected format.');
  const material = client.material_create({
    manifest: {
      context_path: path,
      files: [
        { name: 'old-orders.csv', role: 'reference' as const },
        { name: 'historical-input.csv', role: 'input' as const },
        { name: 'current-orders.csv', role: 'input' as const },
        { name: 'format.md', role: 'reference' as const },
        { name: 'confirmed-output.csv', role: 'artifact' as const },
      ].map(({ name, role }) => {
        const file = join(directory, name);
        writeFileSync(
          file,
          name === 'old-orders.csv'
            ? 'order_id,ordered,received\nA-12,4,7\n'
            : name,
        );
        return { path: file, role };
      }),
    },
  });
  const [oldReference, oldInput, current, shared, historicalOutput] =
    material.files.slice(1).map((file) => file.locator);
  await assert.rejects(
    client.execution_create({
      material_ids: [material.id],
      policy: {
        artifact_confirmed: true,
        confirmation_ref: 'test',
        test_task: {
          instruction: 'Generate result.txt',
          input_locators: [historicalOutput],
        },
      },
    }),
    /input_locators/,
  );
  const execution = await client.execution_create({
    material_ids: [material.id],
    policy: {
      artifact_confirmed: true,
      confirmation_ref: 'test',
      test_task: {
        instruction: 'Generate result.txt',
        input_locators: [current],
        shared_reference_locators: [shared],
        checks: [{ kind: 'text_equals', path: 'result.txt', value: '0x2a' }],
      },
    },
  });
  let generationCalls = 0;
  let continuationId: string | null = null;
  try {
    await client.run_pending({
      runtime: {
        async analyze(context) {
          const hasAnswer = Object.keys(context.answers as object).length > 0;
          return {
            recipe_name: hasAnswer ? 'Attempted rename' : 'Format numbers',
            sufficient_information: hasAnswer,
            hypothesis: hasAnswer ? null : 'Use decimal temporarily',
            questions: hasAnswer
              ? []
              : [
                  {
                    id: 'format',
                    question: 'Which format?',
                    assumption: 'decimal',
                    options: [
                      { id: 'decimal', label: 'Decimal' },
                      { id: 'hex', label: 'Hexadecimal' },
                    ],
                  },
                  {
                    id: 'precision',
                    question: 'Which precision?',
                    assumption: 'exact',
                    options: [
                      { id: 'exact', label: 'Exact' },
                      { id: 'rounded', label: 'Rounded' },
                    ],
                  },
                ],
            rubrics: {
              criteria: [
                {
                  id: 'format',
                  description: context.portability_feedback
                    ? 'Use the selected format'
                    : 'Replay the historical order A-12',
                  source: 'task',
                  evidence: [
                    {
                      material_id: material.id,
                      locator: material.files[0].locator,
                    },
                  ],
                },
              ],
            },
          };
        },
        async generate(context, files) {
          assert.equal(context.recipe_name, 'Format numbers');
          assert.equal(
            client.execution_get(execution.id).name,
            'Format numbers',
          );
          assert.equal(client.recipe_list()[0].name, 'Format numbers');
          const locators = files.map((file) => file.locator);
          assert.ok(
            [oldReference, oldInput, shared, historicalOutput].every(
              (locator) => locators.includes(locator),
            ),
          );
          assert.ok(!locators.includes(current));
          assert.ok(!('test_task' in (context.policy as object)));
          if (!context.portability_feedback) {
            return {
              name: 'Number format',
              description: 'Format a number',
              instructions: 'Read old-orders.csv as a worked example.',
            };
          }
          generationCalls++;
          if (generationCalls === 1) {
            const first = await client.execution_clarification_answer(
              execution.id,
              {
                question_id: 'format',
                question_revision: 1,
                selected_option_ids: ['hex'],
                idempotency_key: 'format-answer',
              },
            );
            assert.equal(first.continuation_execution_id, null);
            assert.equal(client.execution_get(execution.id).input_revision, 1);
            assert.equal(client.execution_get(execution.id).status, 'running');
            assert.equal(client.execution_list().length, 1);
            const last = {
              question_id: 'precision',
              question_revision: 1,
              selected_option_ids: ['exact'],
              idempotency_key: 'precision-answer',
            };
            const [done, replay] = await Promise.all([
              client.execution_clarification_answer(execution.id, last),
              client.execution_clarification_answer(execution.id, last),
            ]);
            assert.deepEqual(done, replay);
            continuationId = done.continuation_execution_id;
            assert.ok(continuationId);
            assert.equal(client.execution_list().length, 2);
          }
          return {
            name: 'Number format',
            description: 'Format a number',
            instructions:
              generationCalls === 1 ? 'Write decimal' : 'Write hexadecimal',
          };
        },
        async execute(context, files, workspace) {
          assert.doesNotMatch(JSON.stringify(context), /A-12|old-orders\.csv/);
          assert.deepEqual(
            files.map((file) => file.locator),
            [current, shared],
          );
          assert.equal(
            (context.recipe as { instructions: string }).instructions,
            'Write hexadecimal',
          );
          writeFileSync(join(workspace, 'result.txt'), '0x2a');
        },
        async assess(context, files) {
          assert.doesNotMatch(JSON.stringify(context), /A-12|old-orders\.csv/);
          assert.deepEqual(context.rubrics, {
            pass_threshold: 0.8,
            criteria: [
              {
                id: 'format',
                description: 'Use the selected format',
                required: true,
              },
            ],
          });
          const locators = files.map((file) => file.locator);
          assert.ok(
            [current, shared, 'test/result.txt'].every((locator) =>
              locators.includes(locator),
            ),
          );
          assert.ok(
            [oldReference, oldInput, historicalOutput].every(
              (locator) => !locators.includes(locator),
            ),
          );
          return {
            verifiable: true,
            scores: [
              { criterion_id: 'format', score: 1, evidence: 'test/result.txt' },
            ],
            reason: 'Correct format',
          };
        },
      },
    });
    const parent = client.execution_get(execution.id);
    assert.equal(parent.status, 'cancelled');
    assert.equal(parent.input_revision, 1);
    assert.deepEqual(parent.test_results, []);
    assert.ok(continuationId);
    const result = client.execution_get(continuationId!);
    assert.equal(generationCalls, 2);
    assert.equal(result.base_execution_id, parent.id);
    assert.equal(result.input_revision, parent.input_revision + 1);
    assert.deepEqual(Object.keys(result.answers).sort(), [
      'format:1',
      'precision:1',
    ]);
    assert.equal(result.status, 'completed', JSON.stringify(result.error));
    assert.equal(result.test_results.length, 1);
    assert.equal(result.test_results[0].input_revision, result.input_revision);
    assert.equal(result.result?.published, true);
    assert.equal(result.name, 'Format numbers');
    const recipe = client.recipe_get(result.recipe_id!);
    assert.equal(recipe.name, result.name);
    assert.equal(recipe.selected_version?.content.name, result.name);
    assert.equal(recipe.selected_version?.number, 1);
  } finally {
    client.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('an expired worker lease resumes the graph without regenerating the candidate', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-recovery-'));
  const client = new RSIClient({
    data_dir: join(directory, 'rsi'),
    config: { model: { model: 'fixture' } },
    auto_start: false,
  });
  try {
    const path = join(directory, 'context.md');
    writeFileSync(path, 'Write a verified answer.');
    const material = client.material_create({
      manifest: { context_path: path },
    });
    const execution = await client.execution_create({
      material_ids: [material.id],
      policy: {
        artifact_confirmed: true,
        confirmation_ref: 'test',
        test_task: {
          instruction: 'Create answer.txt',
          checks: [{ kind: 'exists', path: 'answer.txt' }],
        },
      },
    });
    assert.equal(execution.progress_percent, 0);
    let generated = 0;
    const runtime: Runtime = {
      async analyze() {
        return {
          recipe_name: 'Write an answer file',
          sufficient_information: true,
          rubrics: {
            criteria: [
              {
                id: 'exists',
                description: 'The file exists',
                source: 'agent_inference',
              },
            ],
          },
        };
      },
      async generate() {
        generated++;
        return {
          name: 'Write answer',
          description: 'Text output',
          instructions: 'Write answer.txt',
        };
      },
      async execute() {
        throw new Error('simulated worker crash');
      },
      async assess() {
        return {
          verifiable: true,
          reason: 'Verified',
          scores: [
            { criterion_id: 'exists', score: 1, evidence: 'test/answer.txt' },
          ],
        };
      },
    };
    const claim = client.store.claim('old-worker', 30, 2)!;
    await assert.rejects(
      new Engine(client, runtime, 'old-worker', claim.fence, claim.id).run(),
      /simulated worker crash/,
    );
    const before = client.execution_get(execution.id);
    assert.equal(before.status, 'running');
    assert.equal(before.progress_percent, 40);
    assert.equal(before.name, 'Write an answer file');
    assert.equal(generated, 1);
    client.store.db
      .prepare('UPDATE executions SET lease_until = 0 WHERE id = ?')
      .run(execution.id);
    await client.run_pending({
      runtime: {
        ...runtime,
        async execute(_context, _files, workspace) {
          writeFileSync(join(workspace, 'answer.txt'), '42');
        },
      },
    });
    const after = client.execution_get(execution.id);
    assert.equal(after.status, 'completed', JSON.stringify(after.error));
    assert.equal(after.result?.test_status, 'passed');
    assert.equal(after.progress_percent, 100);
    assert.equal(after.recipe_version_id, before.recipe_version_id);
    assert.equal(after.rubrics_version_id, before.rubrics_version_id);
    assert.ok(!('rubrics' in after.analysis!));
    assert.equal(after.name, before.name);
    assert.equal(client.recipe_list()[0].name, before.name);
    assert.equal(generated, 1);
    assert.equal(after.test_results.length, 1);
    const host = new PublicRSIClient({
      data_dir: client.data_dir,
      model_id: 'fixture',
      base_url: 'http://127.0.0.1:1/v1',
      api_key: 'fixture-key',
      config: { auto_start: false },
    });
    try {
      const generatedVersion = (await host.recipe_get(after.recipe_id!, after.recipe_version_id!)).selected_version;
      assert.equal(generatedVersion?.provenance.rubrics_version_id, after.rubrics_version_id);
      const rubrics = await host.rubrics_get(after.recipe_id!, after.recipe_version_id!);
      assert.equal(rubrics.selected_version?.id, after.rubrics_version_id);
      const recipePath = join(directory, 'prior-recipe.md');
      const recipeText = 'Write the response as a plain text file.';
      writeFileSync(recipePath, recipeText);
      const recipeReference = {
        path: recipePath,
        recipe_id: after.recipe_id!,
      };
      const submission = {
        evidence: { context_path: path, files: [] },
        idempotency_key: 'follow-up',
        recipe_reference: recipeReference,
      };
      await assert.rejects(
        host.execution_create({
          ...submission,
          recipe_reference: { ...recipeReference, recipe_id: after.id },
        }),
        /recipe not found/,
      );
      await assert.rejects(
        host.execution_create({
          ...submission,
          recipe_reference: { path: '' },
        }),
        /recipe_reference/,
      );
      const followUp = await host.execution_create(submission);
      assert.equal(followUp.recipe_id, after.recipe_id);
      assert.equal(followUp.base_execution_id, null);
      assert.equal(
        client.execution_get(followUp.id).base_recipe_version_id,
        null,
      );
      assert.ok(followUp.evidence.some((file) => file.role === 'recipe'));
      writeFileSync(recipePath, 'Modified after submission.');
      assert.equal((await host.execution_create(submission)).id, followUp.id);
      assert.equal('execution_update' in host, false);
      assert.equal('rubrics_update' in host, false);
      assert.equal('recipe_update' in host, false);
      assert.equal('recipe_version_list' in host, false);
      assert.equal('recipe_search' in host, false);
      assert.ok(
        (await host.recipe_list()).some((row) => row.recipe_id === after.recipe_id),
      );
      const latest = (await host.recipe_get(
        after.recipe_id!,
        'latest',
      )).selected_version;
      assert.equal(latest?.id, after.recipe_version_id);
      assert.deepEqual(
        (await host.recipe_get(after.recipe_id!, latest!.id)).selected_version,
        latest,
      );
      const feedback = join(directory, 'feedback.md');
      writeFileSync(feedback, 'Keep the answer concise.');
      const extra = { context_path: feedback, files: [] };
      const appended = await host.execution_append_evidence(followUp.id, extra);
      assert.equal(appended.input_revision, followUp.input_revision + 1);
      assert.equal(appended.evidence.length, followUp.evidence.length + 1);
      assert.equal(appended.progress_percent, 0);
      assert.ok(!('policy' in appended));
      assert.ok(!('snapshot' in appended.evidence[0]));
      assert.deepEqual(
        await host.execution_append_evidence(followUp.id, extra),
        appended,
      );
      await assert.rejects(
        host.execution_append_evidence(after.id, extra),
        /terminal inputs are immutable/,
      );
      assert.equal(
        (await host.recipe_list())[0].active_version_id,
        after.recipe_version_id,
      );
      await client.run_pending({
        runtime: {
          ...runtime,
          async generate(context) {
            assert.equal(context.base_recipe, recipeText);
            return runtime.generate();
          },
          async execute(context, files, workspace) {
            assert.ok(!files.some((file) => file.role === 'recipe'));
            assert.ok(!JSON.stringify(context).includes(recipeText));
            writeFileSync(join(workspace, 'answer.txt'), '42');
          },
          async assess(context, files) {
            assert.ok(!files.some((file) => file.role === 'recipe'));
            assert.ok(!JSON.stringify(context).includes(recipeText));
            return runtime.assess();
          },
        },
      });
      const next = (await host.execution_get(followUp.id));
      assert.equal(next.result?.test_status, 'passed');
      assert.equal(next.recipe_id, after.recipe_id);
      const recipe = await host.recipe_get(next.recipe_id!, 'active');
      assert.ok(host.data_dir);
      assert.equal(recipe.storage_path, `recipes/${next.recipe_id}`);
      for (const id of [after.recipe_version_id!, next.recipe_version_id!]) {
        const file = readFileSync(
          join(host.data_dir, recipe.storage_path, id, 'RECIPE.md'),
          'utf8',
        );
        assert.match(file, /^---\nname: .+\ndescription: .+\n---\n\n/);
      }
      assert.equal((await host.execution_create(submission)).id, followUp.id);
      assert.equal(
        client.execution_get(followUp.id).base_recipe_version_id,
        null,
      );
      assert.equal(next.progress_percent, 100);
      assert.equal(
        (await host.execution_list({ execution_ids: [next.id] }))
          .find((row) => row.id === next.id)?.progress_percent,
        100,
      );
      assert.equal(
        (await host.recipe_list())[0].active_version_id,
        next.recipe_version_id,
      );
      assert.equal(
        (await host.recipe_get(after.recipe_id!, after.recipe_version_id!))
          .selected_version?.archived,
        true,
      );
      await assert.rejects(
        async () =>
          host.recipe_version_set_archived(after.recipe_id!, {
            version_id: after.recipe_version_id!,
            archived: false,
          }),
        /only the latest qualified/,
      );
    } finally {
      host.close();
    }
    assert.throws(
      () => client.store.guard(execution.id, 'old-worker', claim.fence),
      /lease expired/,
    );
  } finally {
    client.close();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('rubrics_get resolves historical Recipe revisions and rejects unassociated versions', async (t) => {
  const data_dir = mkdtempSync(join(tmpdir(), 'cosens-rsi-recipe-rubrics-'));
  const queue = new RSIClient({ data_dir, config: { model: { model: 'fixture' } }, auto_start: false });
  const client = new PublicRSIClient({ data_dir, config: null });
  t.after(() => {
    client.close();
    queue.close();
    rmSync(data_dir, { recursive: true, force: true });
  });
  const context_path = join(data_dir, 'context.md');
  writeFileSync(context_path, 'Prepare an output file');
  const material = queue.material_create({ manifest: { context_path } });
  const execution = await queue.execution_create({
    material_ids: [material.id], policy: { artifact_confirmed: true, confirmation_ref: 'fixture' },
  });
  const { store } = queue;
  const recipe = store.createRecipe('Output');
  const imported = store.createRecipe('Imported');
  const content = { name: 'Output', description: 'Fixture', instructions: 'Prepare an output file.' };
  const rubric = store.insert('rubrics', { name: 'Output checks', description: '', latest_version_id: null });
  const original = store.append(rubric, { criteria: [{ id: 'original' }] });
  const second = store.append(rubric, { criteria: [{ id: 'second' }] }, `rubrics:${execution.id}:2`);
  const current = store.append(rubric, { criteria: [{ id: 'current' }] });
  const originalVersion = store.append(recipe, content, undefined, { execution_id: execution.id, input_revision: 1 });
  const secondVersion = store.append(recipe, content, undefined, { execution_id: execution.id, input_revision: 2 });
  const currentVersion = store.append(recipe, content, undefined, { execution_id: execution.id, input_revision: 3 });
  const unknownVersion = store.append(recipe, content, undefined, { execution_id: execution.id, input_revision: 4 });
  const ambiguousVersion = store.append(recipe, content, undefined, { execution_id: execution.id, input_revision: 3 });
  const importedVersion = store.append(imported, content, undefined, { source: 'cloud' });
  const originalTest = {
    id: 'test_fixture', execution_id: execution.id, recipe_id: recipe.id, version_id: originalVersion.id,
    input_revision: 1, round: 1, rubrics_version_id: original.id, status: 'passed' as const,
    score: 1, scores: [], checks: [], artifacts: [],
  };
  store.save({
    ...execution, recipe_id: recipe.id, recipe_version_id: currentVersion.id,
    input_revision: 3, analyzed_input_revision: 3, rubrics_version_id: current.id,
    test_results: [
      { ...originalTest, id: 'test_later', input_revision: 3, rubrics_version_id: current.id },
      originalTest,
      { ...originalTest, id: 'test_conflicting', version_id: secondVersion.id, input_revision: 2 },
      { ...originalTest, id: 'test_ambiguous_1', version_id: ambiguousVersion.id, input_revision: 3 },
      { ...originalTest, id: 'test_ambiguous_2', version_id: ambiguousVersion.id, input_revision: 3, rubrics_version_id: current.id },
    ],
  });

  for (const [recipeVersion, rubricVersion] of [
    [originalVersion, original], [secondVersion, second], [currentVersion, current],
  ]) {
    const result = await client.rubrics_get(recipe.id, recipeVersion.id);
    assert.equal(result.id, rubric.id);
    assert.equal(result.selected_version?.id, rubricVersion.id);
    assert.deepEqual(result.selected_version?.content, rubricVersion.content);
  }
  for (const [recipeId, versionId] of [
    [recipe.id, unknownVersion.id], [recipe.id, ambiguousVersion.id],
    [imported.id, importedVersion.id], [imported.id, originalVersion.id],
  ]) {
    await assert.rejects(client.rubrics_get(recipeId, versionId), { code: 'not_found' });
  }
});

test('recipe_list shows first-time learning and preserves the usable version during revision', async () => {
  const root = mkdtempSync(join(tmpdir(), 'cosens-rsi-recipe-list-'));
  const client = new RSIClient({
    data_dir: join(root, 'db'),
    config: { model: { model: 'fixture', base_url: 'http://127.0.0.1:1/v1' } },
    auto_start: false,
  });
  try {
    const context_path = join(root, 'task.md');
    writeFileSync(context_path, 'Prepare the task output');
    const material = client.material_create({ manifest: { context_path } });
    const execution = await client.execution_create({
      material_ids: [material.id],
      policy: { artifact_confirmed: true, confirmation_ref: 'fixture' },
    });
    let [row] = client.recipe_list();
    assert.equal(row.kind, 'learning');
    assert.equal(row.recipe_id, null);
    assert.equal(row.execution_id, execution.id);
    assert.equal(row.name, '首次学习');
    assert.deepEqual(client.recipe_list({ q: 'task.md' }), []);
    assert.equal(row.status, 'queued');
    assert.deepEqual(client.recipe_list({ active_only: true }), []);
    const learningHost = new PublicRSIClient({
      data_dir: client.data_dir,
      config: null,
    });
    try {
      assert.deepEqual((await learningHost.recipe_list()), [
        {
          recipe_id: null,
          execution_ids: [execution.id],
          name: '首次学习',
          active_version_id: null,
          created_at: execution.created_at,
          updated_at: execution.updated_at,
        },
      ]);
    } finally {
      learningHost.close();
    }

    const content = {
      name: 'Output',
      description: 'Sample',
      instructions: 'Write the output',
    };
    for (const method of [
      'recipe_create',
      'recipe_version_create',
      'rubrics_create',
      'rubrics_version_create',
      'rubrics_version_list',
    ]) {
      assert.equal(method in client, false);
    }
    const seed = client.store.insert('recipe', {
      name: 'Output Recipe',
      description: '',
      latest_version_id: null,
      active_version_id: null,
    });
    const learningOnlyHost = new PublicRSIClient({
      data_dir: client.data_dir,
      config: null,
    });
    try {
      const unassociated = (await learningOnlyHost.recipe_list())
        .find((entry) => entry.recipe_id === seed.id);
      assert.deepEqual(unassociated?.execution_ids, []);
      assert.ok(
        (await learningOnlyHost.recipe_list())
          .some((entry) => entry.recipe_id === null),
      );
      assert.deepEqual(
        (await learningOnlyHost.recipe_list({
          recipe_ids: [seed.id, 'recipe_missing', seed.id],
        })),
        [unassociated],
      );
      assert.deepEqual((await learningOnlyHost.recipe_list({ recipe_ids: [] })), []);
    } finally {
      learningOnlyHost.close();
    }
    const active = client.store.append(seed, content).id;
    const recipe = client.recipe_get(seed.id);
    assert.equal(
      client.store.tx(() =>
        client.store.publishRecipeVersion(recipe.id, active),
      ),
      true,
    );
    const candidate = client.store.append(
      client.recipe_get(recipe.id),
      content,
    );
    client.store.save({
      ...client.execution_get(execution.id),
      base_recipe_version_id: active,
      status: 'running',
      stage: 'generate',
    });

    [row] = client.recipe_list({ q: 'output recipe' });
    assert.equal(client.recipe_list().length, 1);
    assert.equal(row.kind, 'recipe');
    assert.equal(row.status, 'generating');
    assert.equal(row.active_version_id, active);
    assert.equal(row.candidate_version_id, null);

    client.store.save({
      ...client.execution_get(execution.id),
      recipe_id: recipe.id,
      recipe_version_id: candidate.id,
      status: 'completed',
      questions: [
        {
          id: 'format',
          revision: 1,
          question: 'Which format?',
          assumption: 'Text',
          options: [
            { id: 'text', label: 'Text' },
            { id: 'csv', label: 'CSV' },
          ],
        },
      ],
      recipe_id: recipe.id,
      recipe_version_id: candidate.id,
      rubrics_version_id: 'fixture',
      result: {
        test_status: 'failed',
        provisional: true,
        published: false,
      },
    });
    [row] = client.recipe_list({ active_only: true });
    assert.equal(row.status, 'needs_clarification');
    assert.equal(row.pending_question_count, 1);
    assert.equal(row.active_version_number, 1);
    assert.equal(row.latest_version_number, 2);
    assert.equal(row.candidate_version_number, 2);
    assert.equal(Object.hasOwn(row, 'runtime_config'), false);

    await client.execution_clarification_answer(execution.id, {
      question_id: 'format',
      question_revision: 1,
      selected_option_ids: ['text'],
    });
    [row] = client.recipe_list();
    assert.equal(row.status, 'queued');
    assert.notEqual(row.execution_id, execution.id);
    assert.equal(row.pending_question_count, 0);
    assert.equal(row.active_version_id, active);
    const previousContinuationId = row.execution_id!;
    client.execution_cancel(previousContinuationId);
    const host = new PublicRSIClient({
      data_dir: client.data_dir,
      model_id: 'fixture',
      base_url: 'http://127.0.0.1:1/v1',
      api_key: 'fixture-key',
      config: { auto_start: false },
    });
    try {
      const submission = {
        evidence: { context_path, files: [] },
        idempotency_key: 'revise-available-version',
        recipe_reference: { path: context_path, recipe_id: recipe.id },
      };
      const revision = await host.execution_create(submission);
      assert.equal(revision.recipe_id, recipe.id);
      assert.equal(revision.recipe_version_id, null);
      const detail = (await host.recipe_get(recipe.id, 'latest'));
      assert.deepEqual(
        (await host.recipe_list()).filter((entry) => entry.recipe_id === recipe.id),
        [
          {
            recipe_id: recipe.id,
            execution_ids: [revision.id, previousContinuationId, execution.id],
            name: 'Output Recipe',
            active_version_id: active,
            created_at: detail.created_at,
            updated_at: detail.updated_at,
          },
        ],
      );
      assert.equal(
        client.execution_get(revision.id).base_recipe_version_id,
        null,
      );
      await client.execution_update(previousContinuationId, { archived: true });
      assert.ok(
        !(await host.execution_list()).some((row) => row.id === previousContinuationId),
      );
      assert.deepEqual(
        (await host.execution_list({
            execution_ids: [
              previousContinuationId,
              'execution_missing',
              previousContinuationId,
            ],
          }))
          .map((row) => row.id),
        [previousContinuationId],
      );
      assert.deepEqual(
        (await host.execution_list({ execution_ids: [revision.id, execution.id] }))
          .map((row) => row.id),
        [execution.id, revision.id],
      );
      assert.deepEqual((await host.execution_list({ execution_ids: [] })), []);
      assert.deepEqual(
        (await host.recipe_list()).find((entry) => entry.recipe_id === recipe.id)
          ?.execution_ids,
        [revision.id, execution.id],
      );

      client.execution_cancel(revision.id);
      client.store.save({ ...client.execution_get(execution.id), answers: {} });
      const [card] = await host.recipe_list({ recipe_ids: [recipe.id] });
      assert.deepEqual(card.execution_ids, [revision.id, execution.id]);
      assert.equal('status' in card, false);
      assert.equal(card.active_version_id, active);

      // An archived recipe has no available version.
      (await host.recipe_version_set_archived(recipe.id, {
        version_id: active,
        archived: true,
      }));
      await assert.rejects(
        host.execution_create({
          ...submission,
          idempotency_key: 'unavailable',
        }),
        /base Recipe has no available version/,
      );
      assert.equal((await host.execution_create(submission)).id, revision.id);
    } finally {
      host.close();
    }
  } finally {
    client.close();
    rmSync(root, { recursive: true, force: true });
  }
});

test('current-case replay reads the user text without exposing the conversation or original artifact', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-text-replay-'));
  const base_url = 'http://127.0.0.1:1/v1';
  const model_id = 'text-replay-fixture';
  const api_key = 'text-replay-key';
  const { name, value } = bindModelHeaders(base_url, model_id, { Authorization: `Bearer ${api_key}` });
  const previous = process.env[name];
  process.env[name] = value;
  const host = new PublicRSIClient({
    data_dir: directory, model_id, base_url, api_key, config: { auto_start: false },
  });
  const worker = new RSIClient({ data_dir: directory, auto_start: false });
  t.after(() => {
    host.close();
    worker.close();
    if (previous === undefined) delete process.env[name];
    else process.env[name] = previous;
    rmSync(directory, { recursive: true, force: true });
  });
  const todo = '确认本周销售额\n核对退款金额\n发送周报\n';
  const input_text = `保持原顺序写入 todo.md：\n${todo}`;
  const context_path = join(directory, 'context.md');
  const artifact = join(directory, 'todo.md');
  const conversation = `User: ${input_text}\nAssistant: I created the historical output.`;
  writeFileSync(context_path, conversation);
  writeFileSync(artifact, todo);
  const replayCatalogRoles: string[][] = [];
  let replayText: string | undefined;
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    const input = JSON.parse(String(options.body));
    const { evidence_catalog: catalog } = JSON.parse(input.messages[1].content);
    const tools = new Set(input.tools.map((tool: { function: { name: string } }) => tool.function.name));
    const reads = input.messages.filter((message: { role: string }) => message.role === 'tool');
    let call: [string, object] | undefined;
    if (tools.has('FeedbackExtraction')) {
      call = ['FeedbackExtraction', { items: [] }];
    } else if (tools.has('Analysis')) {
      if (reads.length === 0) {
        const context = catalog.find((entry: { role: string }) => entry.role === 'context');
        call = ['read_evidence', { locator: context.locator }];
      } else {
        assert.equal(JSON.parse(reads[0].content).text, conversation);
        call = ['Analysis', {
          recipe_name: 'Write ordered tasks',
          sufficient_information: true,
          rubrics: {
            criteria: [{ id: 'todo', description: 'Preserve task order', source: 'agent_inference' }],
          },
        }];
      }
    } else if (tools.has('RecipeContent')) {
      call = ['RecipeContent', {
        name: 'Write ordered tasks',
        description: 'Create task files',
        instructions: 'Put the supplied tasks into the requested file.',
      }];
    } else if (tools.has('Assessment')) {
      call = ['Assessment', {
        verifiable: true,
        scores: [{ criterion_id: 'todo', score: 1, evidence: 'test/todo.md' }],
        reason: 'Task order preserved',
      }];
    } else if (reads.length === 0) {
      replayCatalogRoles.push(catalog.map((entry: { role: string }) => entry.role));
      if (catalog.length) call = ['read_evidence', { locator: catalog[0].locator }];
    } else if (reads.length === 1) {
      const text: string = JSON.parse(reads[0].content).text;
      replayText = text;
      const [instruction, ...tasks] = text.split('\n');
      const path = instruction.match(/[\w.-]+\.md/)?.[0];
      if (path) call = ['write_output', { path, content: tasks.join('\n') }];
    }
    return Response.json({
      choices: [{
        finish_reason: call ? 'tool_calls' : 'stop',
        message: {
          role: 'assistant',
          content: call ? null : 'Done',
          tool_calls: call ? [{
            id: `call-${reads.length}`,
            type: 'function',
            function: { name: call[0], arguments: JSON.stringify(call[1]) },
          }] : [],
        },
      }],
    });
  });

  const evidence = { context_path, files: [{ path: artifact, role: 'artifact' as const }] };
  const original = await host.execution_create({ evidence, idempotency_key: 'context-only' });
  await worker.run_pending();
  const originalResult = await host.execution_get(original.id);
  assert.equal(originalResult.status, 'completed', JSON.stringify(originalResult.error));
  const [missing] = originalResult.test_results;
  assert.equal(missing.status, 'unverifiable');
  assert.equal(missing.reason, 'Recipe executor produced no verifiable output files');

  const request = { evidence: { ...evidence, input_text }, idempotency_key: 'with-user-text' };
  const withText = await host.execution_create(request);
  assert.equal((await host.execution_create(request)).id, withText.id);
  await assert.rejects(host.execution_create({
    ...request,
    evidence: { ...evidence, input_text: `${input_text}归档周报\n` },
  }), /idempotency key reused with different arguments/);
  await worker.run_pending();
  const result = await host.execution_get(withText.id);
  assert.equal(result.status, 'completed', JSON.stringify(result.error));
  assert.deepEqual(replayCatalogRoles, [[], ['input']]);
  assert.equal(replayText, input_text);
  const [replayed] = result.test_results;
  assert.equal(replayed.status, 'passed');
  assert.deepEqual(replayed.artifacts.map(({ locator }) => locator), ['test/todo.md']);
  assert.equal(readFileSync(replayed.artifacts[0].path, 'utf8'), todo);
});

test('direct model loop preserves tool order and correlates every response with its call', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-model-wire-'));
  const key = 'COSENS_RSI_FIXTURE_KEY';
  const previous = process.env[key];
  process.env[key] = 'fixture-key';
  const client = new RSIClient({
    data_dir: join(directory, 'db'),
    config: {
      model: {
        model: 'fixture',
        base_url: 'http://127.0.0.1:1/v1/',
        api_key_env: key,
      },
    },
    auto_start: false,
  });
  const workspace = join(directory, 'outputs');
  const trace = join(directory, 'trace.json');
  let requests = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(String(url), 'http://127.0.0.1:1/v1/chat/completions');
    const input = JSON.parse(String(options.body));
    if (++requests === 1) {
      return Response.json({
        choices: [
          {
            finish_reason: 'tool_calls',
            message: {
              role: 'assistant',
              content: null,
              reasoning_content: 'provider continuation',
              tool_calls: [
                {
                  id: 'write',
                  type: 'function',
                  function: {
                    name: 'write_output',
                    arguments: '{"path":"answer.txt","content":"42"}',
                  },
                },
                {
                  id: 'read',
                  type: 'function',
                  function: {
                    name: 'read_output',
                    arguments: '{"path":"answer.txt"}',
                  },
                },
                {
                  id: 'bad',
                  type: 'function',
                  function: {
                    name: 'write_output',
                    arguments: '{broken',
                  },
                },
              ],
            },
          },
        ],
      });
    }
    assert.equal(input.messages[2].reasoning_content, 'provider continuation');
    const [write, read, bad] = input.messages.slice(3);
    assert.equal(write.tool_call_id, 'write');
    assert.equal(JSON.parse(write.content).path, 'answer.txt');
    assert.equal(read.tool_call_id, 'read');
    assert.equal(read.content, '42');
    assert.equal(bad.tool_call_id, 'bad');
    assert.match(bad.content, /Invalid JSON/);
    return Response.json({
      choices: [
        {
          finish_reason: 'stop',
          message: {
            role: 'assistant',
            content: 'Done',
            tool_calls: null,
          },
        },
      ],
    });
  });
  try {
    mkdirSync(workspace);
    const runtime = new ChatRuntime(client, 'fixture', () => {});
    assert.deepEqual(await runtime.execute({}, [], workspace, trace), {
      summary: 'Done',
      reads: [],
    });
    assert.equal(requests, 2);
    assert.equal(readFileSync(join(workspace, 'answer.txt'), 'utf8'), '42');
    assert.doesNotMatch(
      readFileSync(trace, 'utf8'),
      /provider continuation|fixture-key/,
    );
  } finally {
    client.close();
    if (previous === undefined) delete process.env[key];
    else process.env[key] = previous;
    rmSync(directory, { recursive: true, force: true });
  }
});
