import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { setTimeout as sleep } from 'node:timers/promises';
import { RSIClient } from 'cosens-sdk/rsi';
import { RSIClient as QueueClient } from '../src/rsi/client.js';
import { LocalConfigSchema, parseStoredLocalConfig, parseStoredRubrics, RubricsSchema } from '../src/rsi/models.js';
import { runHostScript } from '../src/rsi/test-host.js';

const hostPythonAvailable = ['python3', 'python', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10'].some((command) =>
  spawnSync(command, ['-c', 'import sys; sys.exit(not (sys.version_info >= (3, 10)))'], {
    timeout: 5_000,
    stdio: 'ignore',
  }).status === 0,
);

const report = 'channel,sales\nonline,28\nstore,14\ntotal,42\n';
const hostPythonScript = `
import csv, os, sys
from pathlib import Path

assert not any(name.startswith("RSI_") for name in os.environ)
with Path(sys.argv[1]).open() as source:
    rows = list(csv.DictReader(source))
    total = sum(int(row["sales"]) for row in rows)
Path("report.csv").write_bytes(Path(sys.argv[1]).read_bytes() + f"total,{total}\\n".encode())
print("Host Python computed the total")
`;
const hostTypeScript = `
import { readFileSync, writeFileSync } from 'node:fs';
if (Object.keys(process.env).some((key) => key.startsWith('RSI_'))) throw new Error('Unexpected runner environment');
const input: string = readFileSync(process.argv[2], 'utf8');
const values: number[] = input.trim().split('\\n').slice(1).map((row) => Number(row.split(',')[1]));
writeFileSync('report.csv', input + 'total,' + values.reduce((a, b) => a + b, 0) + '\\n');
console.log('Host TypeScript computed the total');
`;

test('stored Rubrics ignore the former Python flag without changing their criteria', () => {
  const criteria = [{
    id: 'total',
    description: 'Verify the sales total',
    source: 'agent_inference',
    evidence: [],
    required: true,
  }];
  const legacy = { criteria, pass_threshold: 0.9, requires_python: true };
  assert.equal(RubricsSchema.safeParse(legacy).success, false);
  assert.deepEqual(parseStoredRubrics(legacy), { criteria, pass_threshold: 0.9 });
});

test('only stored replay configuration accepts the retired script settings', () => {
  const clean = LocalConfigSchema.parse({ model: { model: 'fixture' } });
  const legacy = { ...clean, test_script_mode: 'host', test_python_image: 'old-image' };
  assert.equal(LocalConfigSchema.safeParse(legacy).success, false);
  assert.deepEqual(parseStoredLocalConfig(legacy), clean);
  assert.throws(() => parseStoredLocalConfig({ ...legacy, unknown_option: true }));
});

test('host replay stops the process group when its lease is lost', {
  skip: process.platform === 'win32',
  timeout: 10_000,
}, async () => {
  const root = mkdtempSync(join(tmpdir(), 'cosens-rsi-host-lease-'));
  const scripts = join(root, 'scripts');
  const workspace = join(root, 'outputs');
  const heartbeat = join(root, 'heartbeat');
  const pidFile = join(root, 'child-pid');
  try {
    mkdirSync(scripts);
    mkdirSync(workspace);
    const childProgram = `
const fs = require('node:fs');
fs.writeFileSync(process.argv[1], String(process.pid));
setInterval(() => fs.appendFileSync(process.argv[2], '.'), 20);
`;
    writeFileSync(join(scripts, 'lease.ts'), `
import { spawn } from 'node:child_process';
spawn(process.execPath, ['-e', ${JSON.stringify(childProgram)}, ...process.argv.slice(2)], { stdio: 'inherit' });
setInterval(() => {}, 10_000);
`);
    const checkAlive = () => {
      if (existsSync(heartbeat)) throw new Error('test lease lost');
    };
    await assert.rejects(runHostScript(
      'typescript', 'scripts/lease.ts', [pidFile, heartbeat], scripts, workspace,
      [], root, LocalConfigSchema.parse({ model: { model: 'fixture' } }), checkAlive,
    ), /test lease lost/);
    const lastWrite = readFileSync(heartbeat, 'utf8');
    await sleep(100);
    assert.equal(readFileSync(heartbeat, 'utf8'), lastWrite);
  } finally {
    if (existsSync(pidFile)) {
      const pid = Number(readFileSync(pidFile, 'utf8'));
      try { process.kill(pid, 'SIGKILL'); } catch { /* Already stopped. */ }
    }
    rmSync(root, { recursive: true, force: true });
  }
});

for (const mode of ['python', 'typescript', 'nonzero', 'text', 'unverified', 'legacy-configuration'] as const) {
  test(`default host recipe replay: ${mode}`, {
    skip: mode === 'python' && !hostPythonAvailable,
  }, async () => {
    const root = mkdtempSync(join(tmpdir(), 'cosens-rsi-host-test-'));
    const data_dir = join(root, 'rsi');
    const contextPath = join(root, 'context.md');
    const current = join(root, 'current.csv');
    const history = join(root, 'prior.csv');
    const needsRealExecution = mode === 'unverified';
    const usesTypeScript = mode === 'typescript' || mode === 'nonzero' || mode === 'legacy-configuration';
    const attemptsScript = mode === 'python' || usesTypeScript;
    const criterionDescription = needsRealExecution
      ? 'Run Python to calculate and verify the total'
      : 'Calculate and verify the total';
    writeFileSync(contextPath, needsRealExecution
      ? '必须用 Python 计算销售额并执行校验。'
      : '计算净销售额，输出 CSV 汇总，并核对各渠道与总计。');
    writeFileSync(current, 'channel,sales\nonline,28\nstore,14\n');
    writeFileSync(history, 'channel,sales\nhistoric,5000\n');
    let assessments = 0;
    const server = createServer(async (req, res) => {
      try {
        let body = '';
        for await (const chunk of req) body += String(chunk);
        const request = JSON.parse(body);
        const tools = new Set(request.tools.map((tool: { function: { name: string } }) => tool.function.name));
        const messages = request.messages as { role: string; content?: string }[];
        const { context, evidence_catalog: catalog } = JSON.parse(messages[1].content!);
        const outputs = messages.filter((message) => message.role === 'tool');
        let call: [string, object] | undefined;
        if (tools.has('FeedbackExtraction')) {
          call = ['FeedbackExtraction', { items: [] }];
        } else if (tools.has('Analysis')) {
          call = ['Analysis', {
            recipe_name: 'Calculate sales totals',
            sufficient_information: true,
            rubrics: {
              criteria: [{
                id: 'verified-total', description: criterionDescription,
                source: 'agent_inference',
              }],
            },
          }];
        } else if (tools.has('RecipeContent')) {
          call = ['RecipeContent', {
            name: 'Calculate sales totals',
            description: 'Summarize tabular sales',
            instructions: criterionDescription,
          }];
        } else if (tools.has('Assessment')) {
          assessments++;
          if (mode === 'unverified' && outputs.length === 0) {
            call = ['read_evidence', { locator: 'trace/executor.json' }];
          } else if (mode === 'unverified') {
            const trace = JSON.parse(JSON.parse(outputs[0].content!).text);
            assert.equal(trace.python_runs, undefined);
            assert.equal(trace.typescript_runs, undefined);
            call = ['Assessment', {
              verifiable: false,
              scores: [{
                criterion_id: 'verified-total', status: 'unverified',
                score: null, evidence: 'No successful Python execution was observed.',
              }],
              reason: 'Required script execution could not be verified',
            }];
          } else if (outputs.length === 0) {
            call = ['read_evidence', { locator: 'test/report.csv' }];
          } else {
            assert.equal(JSON.parse(outputs[0].content!).text, report);
            call = ['Assessment', {
              verifiable: true,
              scores: [{ criterion_id: 'verified-total', score: 1, evidence: 'Verified report.csv' }],
              reason: 'Computed and verified',
            }];
          }
        } else {
          assert.equal(tools.has('run_python'), true);
          assert.equal(tools.has('run_typescript'), true);
          assert.deepEqual(context.criteria, [
            { id: 'verified-total', description: criterionDescription, required: true },
          ]);
          assert.deepEqual(catalog.map((entry: { role: string }) => entry.role), ['input']);
          assert.match(catalog[0].input_path, /^\.\.\/inputs\/material_[^/]+\/1\/current\.csv$/);
          if (attemptsScript) {
            const path = usesTypeScript ? 'scripts/stats.ts' : 'scripts/stats.py';
            if (outputs.length === 0) {
              const content = usesTypeScript ? hostTypeScript : hostPythonScript;
              call = ['write_output', {
                path,
                content: mode === 'nonzero' ? content + '\nprocess.exit(4);\n' : content,
              }];
            } else if (outputs.length === 1) {
              call = [usesTypeScript ? 'run_typescript' : 'run_python', {
                path, args: [catalog[0].input_path],
              }];
            } else {
              const receipt = JSON.parse(outputs[1].content!);
              assert.equal(receipt.status, 'completed');
              assert.equal(receipt.exit_code, mode === 'nonzero' ? 4 : 0);
            }
          } else if (outputs.length === 0) {
            call = ['write_output', { path: 'report.csv', content: report }];
          }
        }
        res.setHeader('Content-Type', 'application/json');
        res.end(JSON.stringify({
          choices: [{ message: call === undefined
            ? { role: 'assistant', content: 'Finished' }
            : {
              role: 'assistant',
              tool_calls: [{
                id: `call-${messages.length}`, type: 'function',
                function: { name: call[0], arguments: JSON.stringify(call[1]) },
              }],
            } }],
        }));
      } catch (error) {
        res.statusCode = 500;
        res.end(String(error));
      }
    });
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const client = new RSIClient({
      data_dir,
      model_id: 'python-runner-fixture',
      base_url: `http://127.0.0.1:${(server.address() as AddressInfo).port}/v1`,
      api_key: 'fixture',
      ...(mode === 'legacy-configuration' ? { config: { auto_start: false } } : {}),
    });
    const reader = new QueueClient({ data_dir, auto_start: false });
    try {
      const started = await client.execution_create({
        evidence: { context_path: contextPath, files: [
          { path: current, role: 'input' },
          { path: history, role: 'reference' },
        ] },
        idempotency_key: mode,
      });
      if (mode === 'legacy-configuration') {
        const queued = reader.execution_get(started.id);
        const runtime_config = {
          ...queued.runtime_config,
          test_script_mode: 'host',
          test_python_image: 'retired-image',
        };
        reader.store.save({ ...queued, runtime_config });
        const binding = runtime_config.model.api_key_env;
        const previous = process.env[binding];
        process.env[binding] = JSON.stringify({ authorization: 'Bearer fixture' });
        try {
          assert.deepEqual(await reader.run_pending(), { processed: 1 });
        } finally {
          if (previous === undefined) delete process.env[binding];
          else process.env[binding] = previous;
        }
      }
      const complete = await reader.wait(started.id, { timeout: 35, poll_interval: 0.05 });
      assert.equal(complete.status, 'completed', JSON.stringify(complete.error));
      const attempt = complete.test_results.at(-1)!;
      const trace = JSON.parse(readFileSync(attempt.trace_path!, 'utf8'));
      assert.doesNotMatch(JSON.stringify(trace), /prior\.csv|historic,5000/);
      if (mode === 'unverified' || mode === 'nonzero') {
        assert.equal(complete.result?.published, false);
        assert.equal(attempt.status, 'unverifiable');
        if (mode === 'unverified') {
          assert.equal(assessments, 2);
          assert.match(attempt.reason!, /script execution could not be verified/);
          assert.equal(trace.python_runs, undefined);
        } else {
          assert.equal(assessments, 0);
          assert.deepEqual(attempt.artifacts, []);
          assert.equal(trace.typescript_runs[0].exit_code, 4);
        }
      } else {
        assert.equal(complete.result?.test_status, 'passed', attempt.reason);
        assert.equal(complete.result?.published, true, attempt.reason);
        assert.equal(attempt.score, 1);
        const recipe = await client.recipe_get(attempt.recipe_id);
        assert.equal(recipe.active_version_id, attempt.version_id);
        assert.deepEqual(attempt.artifacts.map((artifact) => artifact.locator), ['test/report.csv']);
        assert.equal(assessments, 2);
        assert.equal(readFileSync(attempt.artifacts[0].path, 'utf8'), report);
        if (attemptsScript) {
          const receipts = usesTypeScript ? trace.typescript_runs : trace.python_runs;
          assert.equal(receipts.length, 1);
          assert.equal(receipts[0].exit_code, 0);
          assert.match(receipts[0].stdout, /Host (TypeScript|Python) computed the total/);
        } else {
          assert.equal(trace.python_runs, undefined);
          assert.equal(trace.typescript_runs, undefined);
        }
      }
    } finally {
      reader.close();
      client.close();
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      rmSync(root, { recursive: true, force: true });
    }
  });
}
