/** CN gateway smoke test for the new Task workflow. Writes only synthetic data. */
import { randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { CoSensClient } from '../src/index.ts';

export interface SmokeReport {
  status: string;
  region: 'cn';
  openapiVersion?: string;
  workflowPaths?: string[];
  testTag?: string;
  taskId?: string;
  executionId?: string;
  requestSha256?: string;
  inputArtifactId?: string;
  outputArtifactId?: string;
  outputContentHash?: string;
  resultBody?: Record<string, unknown>;
  revisionId?: string;
  error?: string;
}

function persist(outputPath: string | undefined, report: SmokeReport) {
  if (!outputPath) return;
  mkdirSync(dirname(outputPath), { recursive: true });
  writeFileSync(outputPath, JSON.stringify(report, null, 2));
}

export async function validateWorkflow(client: CoSensClient, report: SmokeReport, outputPath?: string): Promise<number> {
  const ready = await client.transport.request('GET', '/health/ready');
  if (ready?.status !== 'ok') throw new Error('CN gateway readiness is not ok');
  const openapi = await client.transport.request('GET', '/openapi.json');
  const paths = Object.keys(openapi?.paths ?? {});
  report.openapiVersion = openapi?.info?.version;
  report.workflowPaths = paths.filter(path => path.startsWith('/v1/tasks') || path.startsWith('/v1/executions') || path.startsWith('/v1/revisions')).sort();
  persist(outputPath, report);
  if (!paths.includes('/v1/tasks')) {
    report.status = 'workflow_not_deployed';
    persist(outputPath, report);
    return 2;
  }

  const tag = `sdk-cn-smoke-${randomUUID().slice(0, 16)}`;
  const sourceText = 'CoSens SDK CN workflow smoke test';
  report.testTag = tag;
  const input = await client.atoms.artifact({ name: `${tag}-input.txt`, content: sourceText });
  report.inputArtifactId = input.artifact_id;
  persist(outputPath, report);
  const task = await client.atoms.createTask({ source: 'new_task', objective: 'Convert the supplied text to uppercase.',
    artifacts: [input.artifact_id], key: `${tag}:task` });
  if (task?.status !== 'ready' || !task.task_id) throw new Error(`synthetic task is not ready: ${task?.status}`);
  report.taskId = task.task_id;
  persist(outputPath, report);
  const execution = await client.atoms.startExecution(task.task_id, { key: `${tag}:execution` });
  if (execution?.status !== 'awaiting_execution' || !execution.execution_id || !execution.request_sha256) {
    throw new Error(`synthetic execution is not ready: ${execution?.status}`);
  }
  report.executionId = execution.execution_id;
  report.requestSha256 = execution.request_sha256;
  persist(outputPath, report);
  const frozen = await client.atoms.getExecution(execution.execution_id);
  if (frozen?.request_sha256 !== execution.request_sha256) throw new Error('frozen execution request hash changed');

  const outputText = sourceText.toUpperCase();
  const output = await client.atoms.artifact({ name: `${tag}-output.txt`, content: outputText });
  report.outputArtifactId = output.artifact_id;
  report.outputContentHash = output.content_hash;
  persist(outputPath, report);
  const body = { requestSha256: execution.request_sha256, outcome: 'completed' as const,
    artifacts: [{ artifact_id: output.artifact_id, role: 'submitted_output' as const }],
    events: [{ id: `${tag}:uppercase`, kind: 'tool_result', payload: { operation: 'uppercase', input_artifact_id: input.artifact_id } }],
    traceCoverage: 'partial' as const };
  report.resultBody = { request_sha256: body.requestSha256, outcome: body.outcome,
    artifacts: body.artifacts, events: body.events, trace_coverage: body.traceCoverage };
  persist(outputPath, report);
  const receipt = await client.atoms.submitExecution(execution.execution_id, body);
  if (receipt?.status !== 'completed' || !receipt.revision_id) throw new Error(`execution submission did not complete: ${receipt?.status}`);
  report.revisionId = receipt.revision_id;
  persist(outputPath, report);
  const replay = await client.atoms.submitExecution(execution.execution_id, body);
  if (replay?.revision_id !== receipt.revision_id) throw new Error('same-body submission did not return the original revision');
  await client.atoms.getRevision(receipt.revision_id);
  const stored = await client.atoms.getArtifact(output.artifact_id);
  if (stored?.content !== outputText || stored?.content_hash !== output.content_hash) {
    throw new Error('stored output artifact differs from the actual host result');
  }
  report.status = 'passed';
  persist(outputPath, report);
  return 0;
}

async function main(): Promise<number> {
  const outputIndex = process.argv.indexOf('--output');
  const outputPath = outputIndex < 0
    ? resolve(dirname(fileURLToPath(import.meta.url)), '../../python/docs/testing/latest-cn/typescript-cn-workflow.json')
    : process.argv[outputIndex + 1];
  if (outputIndex >= 0 && !outputPath) throw new Error('--output requires a path');
  if (outputPath && existsSync(outputPath) && !process.argv.includes('--new-run')) {
    const previous = JSON.parse(readFileSync(outputPath, 'utf8')) as SmokeReport;
    if (previous.testTag && !['passed', 'workflow_not_deployed'].includes(previous.status)) {
      console.error('Earlier synthetic workflow is incomplete; inspect its saved IDs/result body before using --new-run.');
      return 1;
    }
  }
  const apiKey = process.env.RSI_API_KEY_CN ?? process.env.RSI_API_KEY;
  if (!apiKey || apiKey.startsWith('your-')) throw new Error('Configure RSI_API_KEY_CN or RSI_API_KEY');
  const report: SmokeReport = { status: 'running', region: 'cn' };
  const client = new CoSensClient({ apiKey, region: 'cn' });
  try {
    const code = await validateWorkflow(client, report, outputPath);
    console.log(JSON.stringify({ status: report.status, openapiVersion: report.openapiVersion,
      workflowPathCount: report.workflowPaths?.length ?? 0, outputPath: outputPath ?? null }));
    return code;
  } catch (error) {
    report.status = 'failed';
    report.error = `${error instanceof Error ? error.name + ': ' + error.message : String(error)}`.split(apiKey).join('[REDACTED]');
    persist(outputPath, report);
    console.error(JSON.stringify({ status: 'failed', error: report.error, outputPath: outputPath ?? null }));
    return 1;
  } finally {
    await client.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main();
}
