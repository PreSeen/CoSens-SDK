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

for (const mode of ['run', 'skip', 'unavailable', 'unconfigured', 'nonzero', 'text'] as const) {
  test(`local recipe test Python runner: ${mode}`, {
    skip: (mode === 'run' || mode === 'nonzero') && !dockerAvailable,
  }, async () => {
    const root = mkdtempSync(join(tmpdir(), 'cosens-rsi-python-test-'));
    const data_dir = join(root, 'rsi');
    const contextPath = join(root, 'context.md');
    const current = join(root, 'current.csv');
    const history = join(root, 'prior.csv');
    const requiresPython = mode !== 'text';
    writeFileSync(contextPath, requiresPython
      ? 'The task explicitly requires Python to calculate sales totals and run automated checks.'
      : 'The task requires a sales report based on the supplied data.');
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
        const catalog = JSON.parse(messages[1].content!).evidence_catalog;
        const outputs = messages.filter((message) => message.role === 'tool');
        let call: [string, object] | undefined;
        if (tools.has('FeedbackExtraction')) {
          call = ['FeedbackExtraction', { items: [] }];
        } else if (tools.has('Analysis')) {
          call = ['Analysis', {
            recipe_name: 'Calculate sales totals',
            sufficient_information: true,
            rubrics: {
              requires_python: requiresPython,
              criteria: [{
                id: 'verified-total', description: 'Calculate and verify the total',
                source: 'agent_inference',
              }],
            },
          }];
        } else if (tools.has('RecipeContent')) {
          call = ['RecipeContent', {
            name: 'Calculate sales totals',
            description: 'Summarize tabular sales',
            instructions: requiresPython
              ? 'Run a Python script to aggregate by channel and verify the result.'
              : 'Aggregate sales by channel and verify the result.',
          }];
        } else if (tools.has('Assessment')) {
          assessments++;
          if (outputs.length === 0) {
            call = ['read_evidence', { locator: 'test/report.csv' }];
          } else if (requiresPython && outputs.length === 1) {
            assert.match(outputs[0].content!, /online,28\\r\\nstore,14\\r\\ntotal,42/);
            call = ['read_evidence', { locator: 'trace/executor.json' }];
          } else {
            if (requiresPython) {
              assert.match(outputs[1].content!, /Input count, data isolation, network and filesystem checks passed/);
            } else {
              assert.equal(JSON.parse(outputs[0].content!).text, report);
            }
            call = ['Assessment', {
              verifiable: true,
              scores: [{ criterion_id: 'verified-total', score: 1, evidence: 'Verified report.csv and actual exit code' }],
              reason: 'Computed and verified',
            }];
          }
        } else {
          assert.notEqual(mode, 'unconfigured', 'The executor should not start without required Python');
          assert.equal(tools.has('run_python'), requiresPython);
          assert.deepEqual(catalog.map((entry: { role: string }) => entry.role), ['input']);
          if (!requiresPython) {
            assert.equal(catalog[0].sandbox_path, undefined);
            if (outputs.length === 0) {
              call = ['write_output', { path: 'report.csv', content: report }];
            } else if (outputs.length === 1) {
              call = ['run_python', { path: 'scripts/unneeded.py' }];
            } else {
              assert.equal(outputs[1].content, 'Unknown or unauthorized tool.');
            }
          } else if (outputs.length === 0 && mode === 'skip') {
            call = ['write_output', { path: 'report.csv', content: report }];
          } else if (outputs.length === 0) {
            const code = mode === 'nonzero' ? script + '\nraise SystemExit(4)\n' : script;
            call = ['write_output', { path: 'scripts/stats.py', content: code }];
          } else if (outputs.length === 1 && mode !== 'skip') {
            assert.match(catalog[0].sandbox_path, /^\/workspace\/inputs\/material_[^/]+\/1\/current\.csv$/);
            call = ['run_python', { path: 'scripts/stats.py', args: [catalog[0].sandbox_path] }];
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
        ...(mode === 'unconfigured' || mode === 'text' ? {} : {
          test_python_image: mode === 'unavailable' ? 'cosens-rsi-test:missing-' + process.pid : image,
        }),
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
      if (mode === 'unconfigured') {
        assert.equal(complete.result?.published, false);
        assert.equal(assessments, 0);
        assert.equal(attempt.status, 'unverifiable');
        assert.match(attempt.reason!, /test_python_image/);
        assert.equal(attempt.trace_path, undefined);
        assert.deepEqual(attempt.artifacts, []);
        return;
      }
      const trace = JSON.parse(readFileSync(attempt.trace_path!, 'utf8'));
      assert.doesNotMatch(JSON.stringify(trace), /prior\.csv|historic,5000/);
      if (mode === 'run' || mode === 'text') {
        assert.equal(complete.result?.published, true, attempt.reason);
        assert.equal(complete.result?.test_status, 'passed');
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
          assert.equal(trace.python_runs, undefined);
        }
      } else {
        assert.equal(complete.result?.published, false);
        assert.equal(assessments, 0);
        if (mode === 'skip') {
          assert.equal(attempt.status, 'failed');
          assert.match(attempt.reason!, /Python execution was skipped/);
          assert.equal(trace.python_runs, undefined);
        } else if (mode === 'unavailable') {
          assert.equal(attempt.status, 'unverifiable');
          assert.equal(complete.test_results.length, 1);
          assert.equal(trace.python_runs[0].status, 'unavailable');
          assert.equal(trace.python_runs[0].exit_code, null);
        } else {
          assert.equal(attempt.status, 'failed');
          assert.deepEqual(attempt.artifacts, []);
          assert.equal(trace.python_runs[0].exit_code, 4);
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
