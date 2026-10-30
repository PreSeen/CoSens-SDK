import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient } from '../src/index.ts';
import { validateWorkflow, type SmokeReport } from '../examples/05_task_workflow_cn_smoke.ts';

test('CN workflow smoke makes no writes when new routes are absent', async () => {
  const methods: string[] = [];
  const client = new CoSensClient({ apiKey: 'test', region: 'cn', fetch: async (input, init) => {
    methods.push(init?.method ?? 'GET');
    const path = new URL(String(input)).pathname;
    return new Response(JSON.stringify(path.endsWith('/health/ready') ? { status: 'ok' } : { info: { version: '1.0.0' }, paths: {} }));
  } });
  const report: SmokeReport = { status: 'running', region: 'cn' };
  assert.equal(await validateWorkflow(client, report), 2);
  assert.equal(report.status, 'workflow_not_deployed');
  assert.deepEqual(methods, ['GET', 'GET']);
});

test('CN workflow smoke submits observed output and retries an identical result body', async () => {
  const submitted: unknown[] = [];
  const routes: string[] = [];
  const expectedOutput = 'COSENS SDK CN WORKFLOW SMOKE TEST';
  const client = new CoSensClient({ apiKey: 'test', region: 'cn', fetch: async (input, init) => {
    const url = new URL(String(input));
    const path = url.pathname;
    routes.push(path);
    assert.equal(new Headers(init?.headers).get('x-auth-type'), 'api_key');
    assert.equal(new Headers(init?.headers).get('authorization'), 'Bearer test');
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    if (path.endsWith('/health/ready')) return new Response(JSON.stringify({ status: 'ok' }));
    if (path.endsWith('/openapi.json')) return new Response(JSON.stringify({ info: { version: '2.0.0' }, paths: { '/v1/tasks': {} } }));
    if (path.endsWith('/v1/artifacts')) {
      const id = body.name.endsWith('input.txt') ? 'a1' : 'a2';
      return new Response(JSON.stringify({ artifact_id: id, content_hash: `hash-${id}` }));
    }
    if (path.endsWith('/v1/tasks')) {
      assert.equal(body.source, 'new_task');
      assert.deepEqual(body.artifacts, ['a1']);
      return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    }
    if (path.endsWith('/v1/tasks/t1/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'frozen-hash', request: { kind: 'original' } }));
    if (path.endsWith('/v1/executions/e1/result')) {
      submitted.push(body);
      return new Response(JSON.stringify({ status: 'completed', revision_id: 'r1' }));
    }
    if (path.endsWith('/v1/executions/e1')) return new Response(JSON.stringify({ execution_id: 'e1', request_sha256: 'frozen-hash' }));
    if (path.endsWith('/v1/revisions/r1')) return new Response(JSON.stringify({ revision_id: 'r1' }));
    if (path.endsWith('/v1/artifacts/a2')) return new Response(JSON.stringify({ artifact_id: 'a2', content_hash: 'hash-a2', content: expectedOutput }));
    throw new Error(path);
  } });
  const report: SmokeReport = { status: 'running', region: 'cn' };
  assert.equal(await validateWorkflow(client, report), 0);
  assert.equal(report.status, 'passed');
  assert.equal(report.revisionId, 'r1');
  assert.equal(submitted.length, 2);
  assert.deepEqual(submitted[0], submitted[1]);
  assert.deepEqual(submitted[0], report.resultBody);
  assert.deepEqual((submitted[0] as any).artifacts, [{ artifact_id: 'a2', role: 'submitted_output' }]);
  assert.equal(routes.filter(path => path.endsWith('/v1/executions/e1/result')).length, 2);
});
