import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import type { AddressInfo } from 'node:net';
import Database from 'better-sqlite3';
import { RSIClient, type Runtime } from '../src/rsi/index.js';
import { Engine } from '../src/rsi/engine.js';

test('independent Node hooks enqueue work that persists and passes a file test after the hooks exit', async () => {
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
              ],
            },
          },
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
        assert.ok(context.objective_checks[0].passed);
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
    } catch {
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
      import { RSIClient } from 'cosens-sdk/rsi';
      const [data_dir, material_id, key] = process.argv.slice(1);
      const client = new RSIClient({ data_dir });
      const execution = await client.execution_create({
        material_ids: [material_id], idempotency_key: key,
        policy: { artifact_confirmed: true, confirmation_ref: 'test', auto_activate: true,
          test_task: { instruction: 'Create answer.txt', checks: [{kind:'text_equals',path:'answer.txt',value:'42'}] } },
      });
      client.close();
      process.stdout.write(execution.id);
    `;
    const runHook = async (source: string, args: string[]) => {
      const child = spawn(
        process.execPath,
        ['--input-type=module', '-e', source, ...args],
        {
          cwd: new URL('..', import.meta.url),
          env: { ...process.env, RSI_MODEL_API_KEY: 'fixture' },
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
    const ids = await Promise.all(
      ['a', 'b', 'c', 'a'].map((key) =>
        runHook(script, [data_dir, material.id, key]),
      ),
    );
    assert.equal(ids[0], ids[3]);
    for (const id of new Set(ids)) {
      const run = await client.wait(id, { timeout: 25, poll_interval: 0.05 });
      assert.equal(run.status, 'completed', JSON.stringify(run.error));
      assert.equal(run.result?.test_status, 'passed');
      assert.deepEqual(
        run.analysis?.attribution?.possible_causes[0].evidence_refs,
        [{ material_id: material.id, locator: material.files[0].locator }],
      );
      assert.deepEqual(
        run.analysis?.feedback?.map((item) => item.evidence_refs[0].locator),
        [material.files[0].locator, material.files[1].locator],
      );
      assert.equal(run.result?.activated, true);
      assert.equal(
        readFileSync(run.test_results[0].artifacts[0].path, 'utf8'),
        '42',
      );
      assert.equal(
        client.recipe_get(run.recipe_id!).selected_version?.id,
        run.recipe_version_id,
      );
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
    assert.equal(peak, 1);
    assert.equal(requests, 33);
    const db = new Database(join(data_dir, 'rsi-ts.sqlite3'), {
      readonly: true,
    });
    try {
      assert.deepEqual(
        db
          .prepare(
            'SELECT kind, count(*) AS n FROM objects GROUP BY kind ORDER BY kind',
          )
          .all(),
        [
          { kind: 'execution', n: 3 },
          { kind: 'material', n: 1 },
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
        const client = new RSIClient({ data_dir: process.argv[1] });
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
      for (const run of recovery.execution_list()) {
        if (run.status === 'running' || run.status === 'queued')
          recovery.execution_cancel(run.id);
      }
      recovery.close();
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

test('new clarification restarts the input revision and only the corrected version is tested', async () => {
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
      auto_activate: true,
      test_task: {
        instruction: 'Generate result.txt',
        input_locators: [current],
        shared_reference_locators: [shared],
        checks: [{ kind: 'text_equals', path: 'result.txt', value: '0x2a' }],
      },
    },
  });
  let generationCalls = 0;
  try {
    await client.run_pending({
      runtime: {
        async analyze(context) {
          const hasAnswer = Object.keys(context.answers as object).length > 0;
          return {
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
              instructions: 'Worked example: A-12,4,7',
            };
          }
          generationCalls++;
          if (generationCalls === 1) {
            await client.execution_clarification_answer(execution.id, {
              question_id: 'format',
              question_revision: 1,
              selected_option_ids: ['hex'],
              idempotency_key: 'format-answer',
            });
          }
          return {
            title: 'Number format',
            applicability: 'Format a number',
            instructions:
              generationCalls === 1 ? 'Write decimal' : 'Write hexadecimal',
          };
        },
        async execute(context, files, workspace) {
          assert.ok(!JSON.stringify(context).includes('A-12'));
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
          assert.ok(!JSON.stringify(context).includes('A-12'));
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
    const result = client.execution_get(execution.id);
    assert.equal(generationCalls, 2);
    assert.equal(result.input_revision, 2);
    assert.equal(result.status, 'completed', JSON.stringify(result.error));
    assert.equal(result.test_results.length, 1);
    assert.equal(result.test_results[0].input_revision, 2);
    assert.equal(result.result?.activated, true);
    assert.equal(client.recipe_version_list(result.recipe_id!).length, 1);
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
    let generated = 0;
    const runtime: Runtime = {
      async analyze() {
        return {
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
    assert.equal(generated, 1);
    client.store.db
      .prepare('UPDATE jobs SET lease_until = 0 WHERE id = ?')
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
    assert.equal(after.recipe_version_id, before.recipe_version_id);
    assert.equal(generated, 1);
    assert.equal(after.test_results.length, 1);
    assert.throws(
      () => client.store.guard(execution.id, 'old-worker', claim.fence),
      /lease expired/,
    );
  } finally {
    client.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
