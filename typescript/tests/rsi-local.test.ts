import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
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
import type { Runtime } from '../src/rsi/models.js';
import { Engine } from '../src/rsi/engine.js';
import { ChatRuntime } from '../src/rsi/runtime.js';
import { legacyRecipes, schema } from '../src/rsi/store-schema.js';

for (const version of [3, 4]) {
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
        } else {
          db.exec(schema);
          db.exec(`
          INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 'Legacy Recipe');
          INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 'Waiting Recipe');
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
        if (version === 4)
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
        assert.equal('revision' in recipe, false);
        assert.equal('description' in recipe, false);
        assert.equal(recipe.selected_version?.id, 'version_second');
        const unversioned = (await host.recipe_get('recipe_unversioned', 'latest'));
        assert.equal('revision' in unversioned, false);
        assert.equal('description' in unversioned, false);
        assert.equal(unversioned.selected_version, null);
        (await host.recipe_version_set_archived(recipeId, {
          version_id: 'version_first',
          archived: false,
        }));
        assert.equal(
          (await host.recipe_list())[0].available_version_id,
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
            ['archived_at', 'created_at', 'id', 'name', 'updated_at'],
          );
          assert.deepEqual(
            migrated
              .prepare('SELECT status FROM versions ORDER BY number')
              .pluck()
              .all(),
            ['available', 'candidate'],
          );
          assert.equal(migrated.pragma('user_version', { simple: true }), 5);
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

test('independent Node hooks enqueue work that persists and passes a file test after the hooks exit', async (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'cosens-rsi-ts-'));
  const data_dir = join(directory, 'rsi');
  let active = 0;
  let peak = 0;
  let requests = 0;
  const server = createServer(async (req, res) => {
    active++;
    peak = Math.max(peak, active);
    requests++;
    try {
      assert.equal(req.url, '/v1/chat/completions');
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const input = JSON.parse(Buffer.concat(chunks).toString());
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
      if (tools.has('FeedbackExtraction') && afterTool) {
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
        assert.ok(
          evidence_catalog.some(
            (entry: { role: string }) => entry.role === 'reference',
          ),
        );
        assert.deepEqual(context.user_feedback, feedback);
        call = {
          name: 'Analysis',
          args: {
            recipe_name: 'Write checked answers',
            sufficient_information: true,
            hypothesis: null,
            questions: [],
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
        call = {
          name: 'RecipeContent',
          args: {
            title: 'Write answer',
            applicability: 'Text output',
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
      const client = new RSIClient({ data_dir, config: { model: model + '-' + key, base_url, auto_start: true } });
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
    const runHook = async (source: string, args: string[]) => {
      const child = spawn(
        process.execPath,
        ['--input-type=module', '-e', source, ...args],
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
            context_path: path,
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
          status: 'passed',
          available_version_id: run.recipe_version_id,
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
        assert.equal(archived?.available_version_id, null);
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
            available!.available_version_id!,
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
    assert.equal(requests, 36);
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
      const execution = await recovery.execution_create({
        material_ids: [source.id],
        policy: {
          artifact_confirmed: true,
          confirmation_ref: 'recovery',
          test_task: {
            instruction: 'Create answer.txt',
            checks: [{ kind: 'text_equals', path: 'answer.txt', value: '42' }],
          },
        },
      });
      assert.ok(recovery.store.reserve('worker', 'stopped-worker', 1, 3));
      assert.equal(recovery.store.claim('stopped-worker', 3, 1)?.fence, 1);
      const reply = await runHook(
        `
        import { RSIClient } from 'cosens-sdk/rsi';
        const client = new RSIClient({ data_dir: process.argv[1], config: null });
        console.log(JSON.stringify(await client.recover()));
        client.close();
      `,
        [recovery.data_dir],
      );
      assert.deepEqual(JSON.parse(reply), { started: 0, scheduled: true });

      const run = await recovery.wait(execution.id, { timeout: 20 });
      assert.equal(run.status, 'completed', JSON.stringify(run.error));
      assert.equal(run.result?.test_status, 'passed');
      assert.ok(run.test_results[0].trace_path?.includes('lease-2'));
    } finally {
      for (const run of (await recovery.execution_list())) {
        if (run.status === 'running' || run.status === 'queued')
          (await recovery.execution_cancel(run.id));
      }
      recovery.close();
    }

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
              title: 'Number format',
              applicability: 'Format a number',
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
            title: 'Number format',
            applicability: 'Format a number',
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
    assert.equal(recipe.selected_version?.content.title, result.name);
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
          title: 'Write answer',
          applicability: 'Text output',
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
      config: {
        model: 'fixture',
        base_url: 'http://127.0.0.1:1/v1',
        auto_start: false,
      },
    });
    try {
      const recipePath = join(directory, 'prior-recipe.md');
      const recipeText = 'Write the response as a plain text file.';
      writeFileSync(recipePath, recipeText);
      const recipeFile = {
        path: recipePath,
        role: 'recipe' as const,
        recipe_id: after.recipe_id!,
      };
      const submission = {
        evidence: { context_path: path, files: [recipeFile] },
        idempotency_key: 'follow-up',
      };
      await assert.rejects(
        host.execution_create({
          ...submission,
          evidence: {
            context_path: path,
            files: [{ ...recipeFile, recipe_id: after.id }],
          },
        }),
        /recipe not found/,
      );
      await assert.rejects(
        host.execution_create({
          ...submission,
          evidence: { context_path: path, files: [recipeFile, recipeFile] },
        }),
        /at most one recipe file/,
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
        (await host.recipe_list())[0].available_version_id,
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
        (await host.recipe_list())[0].available_version_id,
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
          status: 'queued',
          available_version_id: null,
          created_at: execution.created_at,
          updated_at: execution.updated_at,
        },
      ]);
    } finally {
      learningHost.close();
    }

    const content = {
      title: 'Output',
      applicability: 'Sample',
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
      config: {
        model: 'fixture',
        base_url: 'http://127.0.0.1:1/v1',
        auto_start: false,
      },
    });
    try {
      const submission = {
        evidence: {
          context_path,
          files: [
            {
              path: context_path,
              role: 'recipe' as const,
              recipe_id: recipe.id,
            },
          ],
        },
        idempotency_key: 'revise-available-version',
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
            status: 'queued',
            available_version_id: active,
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
