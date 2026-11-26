import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { RSIClient } from 'cosens-sdk/rsi';
import { RSIClient as QueueClient } from '../src/rsi/client.js';
import { parseStoredRubrics, RubricsSchema } from '../src/rsi/models.js';

const image = process.env.RSI_TEST_PYTHON_IMAGE ?? 'python:3.12-slim';
const dockerAvailable = spawnSync('docker', ['image', 'inspect', image], {
  timeout: 5_000,
  stdio: 'ignore',
}).status === 0;

const script = `
import csv
import os
import socket
import sys
from pathlib import Path

input_file = Path(sys.argv[1])
with input_file.open() as source:
    rows = list(csv.DictReader(source))
totals = {row["channel"]: int(row["sales"]) for row in rows}
with Path("report.csv").open("w", newline="") as report:
    writer = csv.writer(report)
    writer.writerow(["channel", "sales"])
    writer.writerows(sorted(totals.items()))
    writer.writerow(["total", sum(totals.values())])
assert len(rows) == len(totals)
assert list(Path("/workspace/inputs").rglob("*.csv")) == [input_file]
assert {name for _, name in socket.if_nameindex()} == {"lo"}
assert os.getuid() == 65534
for path in (input_file, Path("/proc/1/fd/1")):
    try:
        path.open("a").close()
    except OSError:
        pass
    else:
        raise RuntimeError(f"Write allowed: {path}")
print("Input count, data isolation, network and filesystem checks passed")
`;

const report = 'channel,sales\nonline,28\nstore,14\ntotal,42\n';

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

for (const mode of [
  'run', 'nonzero', 'fallback', 'available-unused', 'unconfigured', 'text',
] as const) {
  test(`local recipe test Python runner: ${mode}`, {
    skip: (mode === 'run' || mode === 'nonzero') && !dockerAvailable,
  }, async () => {
    const root = mkdtempSync(join(tmpdir(), 'cosens-rsi-python-test-'));
    const data_dir = join(root, 'rsi');
    const contextPath = join(root, 'context.md');
    const current = join(root, 'current.csv');
    const history = join(root, 'prior.csv');
    const needsRealExecution = mode === 'run' || mode === 'nonzero' || mode === 'unconfigured';
    const pythonConfigured = mode !== 'text' && mode !== 'unconfigured';
    const attemptsPython = mode === 'run' || mode === 'nonzero' || mode === 'fallback';
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
          if (mode === 'unconfigured' && outputs.length === 0) {
            call = ['read_evidence', { locator: 'trace/executor.json' }];
          } else if (mode === 'unconfigured') {
            const trace = JSON.parse(JSON.parse(outputs[0].content!).text);
            assert.equal(trace.python_runs, undefined);
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
          } else if (mode === 'run' && outputs.length === 1) {
            assert.match(outputs[0].content!, /online,28\\r\\nstore,14\\r\\ntotal,42/);
            call = ['read_evidence', { locator: 'trace/executor.json' }];
          } else {
            if (mode === 'run') {
              assert.match(outputs[1].content!, /Input count, data isolation, network and filesystem checks passed/);
            } else {
              assert.equal(JSON.parse(outputs[0].content!).text, report);
            }
            call = ['Assessment', {
              verifiable: true,
              scores: [{ criterion_id: 'verified-total', score: 1, evidence: 'Verified report.csv' }],
              reason: 'Computed and verified',
            }];
          }
        } else {
          assert.equal(tools.has('run_python'), pythonConfigured);
          assert.deepEqual(context.criteria, [
            { id: 'verified-total', description: criterionDescription, required: true },
          ]);
          assert.deepEqual(catalog.map((entry: { role: string }) => entry.role), ['input']);
          if (!attemptsPython) {
            assert.equal(typeof catalog[0].sandbox_path, pythonConfigured ? 'string' : 'undefined');
            if (outputs.length === 0) {
              call = ['write_output', { path: 'report.csv', content: report }];
            }
          } else if (outputs.length === 0) {
            const code = mode === 'nonzero' ? script + '\nraise SystemExit(4)\n' : script;
            call = ['write_output', { path: 'scripts/stats.py', content: code }];
          } else if (outputs.length === 1) {
            assert.match(catalog[0].sandbox_path, /^\/workspace\/inputs\/material_[^/]+\/1\/current\.csv$/);
            call = ['run_python', { path: 'scripts/stats.py', args: [catalog[0].sandbox_path] }];
          } else if (mode === 'fallback' && outputs.length === 2) {
            assert.equal(JSON.parse(outputs[1].content!).status, 'unavailable');
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
      config: {
        auto_start: true,
        ...(pythonConfigured ? {
          test_python_image: mode === 'fallback' ? 'cosens-rsi-test:missing-' + process.pid : image,
        } : {}),
      },
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
      const complete = await reader.wait(started.id, { timeout: 35, poll_interval: 0.05 });
      assert.equal(complete.status, 'completed', JSON.stringify(complete.error));
      const attempt = complete.test_results.at(-1)!;
      const trace = JSON.parse(readFileSync(attempt.trace_path!, 'utf8'));
      assert.doesNotMatch(JSON.stringify(trace), /prior\.csv|historic,5000/);
      if (mode === 'unconfigured' || mode === 'nonzero') {
        assert.equal(complete.result?.published, false);
        assert.equal(attempt.status, 'unverifiable');
        if (mode === 'unconfigured') {
          assert.equal(assessments, 2);
          assert.match(attempt.reason!, /script execution could not be verified/);
          assert.equal(trace.python_runs, undefined);
        } else {
          assert.equal(assessments, 0);
          assert.deepEqual(attempt.artifacts, []);
          assert.equal(trace.python_runs[0].exit_code, 4);
        }
      } else {
        assert.equal(complete.result?.test_status, 'passed', attempt.reason);
        assert.equal(complete.result?.published, true, attempt.reason);
        assert.equal(attempt.score, 1);
        const recipe = await client.recipe_get(attempt.recipe_id);
        assert.equal(recipe.active_version_id, attempt.version_id);
        assert.deepEqual(attempt.artifacts.map((artifact) => artifact.locator), ['test/report.csv']);
        if (mode === 'run') {
          assert.equal(assessments, 3);
          assert.equal(readFileSync(attempt.artifacts[0].path, 'utf8'), report.replaceAll('\n', '\r\n'));
          assert.equal(trace.python_runs.length, 1);
          assert.equal(trace.python_runs[0].exit_code, 0);
          assert.match(trace.python_runs[0].stdout, /checks passed/);
        } else {
          assert.equal(assessments, 2);
          assert.equal(readFileSync(attempt.artifacts[0].path, 'utf8'), report);
          if (mode === 'fallback') {
            assert.equal(trace.python_runs[0].status, 'unavailable');
            assert.equal(trace.python_runs[0].exit_code, null);
          } else {
            assert.equal(trace.python_runs, undefined);
          }
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
