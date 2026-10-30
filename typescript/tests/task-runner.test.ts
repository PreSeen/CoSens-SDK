import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Agent, Runner, tool } from '@openai/agents';
import { ScriptedModel, assistantMessage, functionCall } from '@openai/agents-core/testing';
import { z } from 'zod';
import { cosens, TaskWorkflowRunner } from '../src/index.ts';

class FakeAgent {
  instructions = 'native instructions';
  clone({ instructions }: { instructions: string }) { const copy = new FakeAgent(); copy.instructions = instructions; return copy; }
}

for (const streamed of [false, true]) test(`real Agents approval resumes the same Task execution (streamed=${streamed})`, async () => {
  const model = new ScriptedModel([[functionCall('lookup', { query: 'fact' }, { callId: 'call-1' })], [assistantMessage('approved answer')]]);
  const toolCalls: string[] = [];
  const lookup = tool({ name: 'lookup', description: 'Look up a fact', parameters: z.object({ query: z.string() }), needsApproval: true,
    execute: async ({ query }) => { toolCalls.push(query); return 'found'; } });
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', tools: [lookup], model });
  const seen: string[] = [];
  const wrapped = new TaskWorkflowRunner(new Runner({ tracingDisabled: true }), { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname; seen.push(path);
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    if (path === '/api/v1/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path.endsWith('/result')) return new Response(JSON.stringify({ status: 'completed', revision_id: 'r1' }));
    throw new Error(path);
  } });
  try {
    const interrupted = await wrapped.run(agent, 'work', { stream: streamed,
      rsi: { turnId: 'approved-turn', task: { source: 'new_task', objective: 'work' } } });
    if (streamed) await interrupted.completed;
    assert.equal(interrupted.interruptions.length, 1);
    assert.equal(wrapped.cosens.getTurn('approved-turn')?.state, 'awaiting_native_approval');
    assert.deepEqual(toolCalls, []);
    assert.equal(model.calls.length, 1);
    const pending = await wrapped.pendingState(agent, 'approved-turn');
    pending.approve(pending.getInterruptions()[0]);
    const resumed = await wrapped.resume(agent, 'approved-turn', pending, { stream: streamed });
    if (streamed) await resumed.completed;
    assert.equal(resumed.finalOutput, 'approved answer');
    assert.equal(wrapped.cosens.getTurn('approved-turn')?.state, 'completed');
    assert.deepEqual(toolCalls, ['fact']);
    assert.equal(model.calls.length, 2);
    for (const path of ['/api/v1/tasks', '/api/v1/tasks/t1/executions', '/api/v1/executions/e1/result']) assert.equal(seen.filter(x => x === path).length, 1);
    assert.equal(model.calls[0]!.request.systemInstructions, model.calls[1]!.request.systemInstructions);
    assert.equal(agent.instructions, 'Original instruction');
  } finally { await wrapped.close(); }
});

test('factory recognizes native Runner and disable calls only the host', async () => {
  const selected = cosens(new Runner(), { mode: 'disable', apiKey: 'key', statePath: ':memory:' });
  assert.ok(selected instanceof TaskWorkflowRunner);
  await selected.close();
  let requests = 0;
  let nativeCalls = 0;
  const native = { async run() { nativeCalls++; return { finalOutput: 'native answer', interruptions: [] }; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'disable', apiKey: 'key', statePath: ':memory:', fetch: async () => { requests++; return new Response('{}'); } });
  const result = await wrapped.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(result.finalOutput, 'native answer');
  assert.equal(nativeCalls, 1);
  assert.equal(requests, 0);
  await wrapped.close();
  const keyless = new TaskWorkflowRunner(native, { mode: 'disable' });
  await keyless.run(new FakeAgent(), 'work');
  await keyless.close();
});

test('task Runner returns native result and reconciles identical result body without rerunning host', async () => {
  let nativeCalls = 0;
  let submissions = 0;
  const seen: Array<{ path: string; body: unknown }> = [];
  const native = { async run(agent: any) {
    nativeCalls++;
    assert.match(agent.instructions, /frozen task execution request/);
    return { finalOutput: 'native answer', interruptions: [], newItems: [{ type: 'tool_call_output_item', rawItem: { output: 'observed tool output' } }] };
  } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async (input, init) => {
    const path = new URL(String(input)).pathname;
    seen.push({ path, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path === '/api/v1/tasks/t1/executions') return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'frozen-hash', request: { kind: 'original' } }));
    if (path === '/api/v1/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path === '/api/v1/executions/e1/result') {
      submissions++;
      return submissions === 1 ? new Response(JSON.stringify({ error: { code: 'transient', message: 'receipt lost' } }), { status: 503 }) :
        new Response(JSON.stringify({ status: 'completed', revision_id: 'r1' }));
    }
    throw new Error(path);
  } });
  const rsi = { turnId: 'turn-1', task: { source: 'new_task' as const, objective: 'work' } };
  const result = await wrapped.run(new FakeAgent(), 'work', { rsi });
  assert.equal(result.finalOutput, 'native answer');
  assert.equal(wrapped.cosens.getTurn('turn-1')?.state, 'completion_pending');
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi }), /may have executed/);
  const recovered = await wrapped.reconcile('turn-1');
  assert.equal(recovered.revisionId, 'r1');
  assert.equal(nativeCalls, 1);
  assert.equal(seen.filter(x => x.path === '/api/v1/artifacts').length, 1);
  const bodies = seen.filter(x => x.path.endsWith('/result')).map(x => x.body);
  assert.deepEqual(bodies[0], bodies[1]);
  assert.deepEqual(bodies[0], { request_sha256: 'frozen-hash', outcome: 'completed', artifacts: [{ artifact_id: 'a1', role: 'submitted_output' }],
    events: [{ id: 'native-item-1', kind: 'tool_call_output_item', payload: { item_type: 'tool_call_output_item', raw_item: { output: 'observed tool output' } } }], trace_coverage: 'partial' });
  await wrapped.close();
});

test('task Runner resumes a context gap on the same task and submits host-observed events', async () => {
  let ready = false;
  let nativeCalls = 0;
  const seen: string[] = [];
  const native = { async run() { nativeCalls++; return { finalOutput: 'native answer', interruptions: [] }; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:',
    traceAdapter: () => ({ events: [{ id: 'event-1', kind: 'tool_result', payload: { name: 'tool' } }], traceCoverage: 'partial' }),
    fetch: async (input, init) => {
      const path = new URL(String(input)).pathname; seen.push(path);
      if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'needs_context' }));
      if (path === '/api/v1/tasks/t1/context') { ready = true; return new Response(JSON.stringify({ task_id: 't1', status: 'ready' })); }
      if (path === '/api/v1/tasks/t1') return new Response(JSON.stringify({ task_id: 't1', status: ready ? 'ready' : 'needs_context' }));
      if (path === '/api/v1/tasks/t1/executions') return new Response(JSON.stringify({ execution_id: 'e1', request_sha256: 'h1', request: { kind: 'reconstructed' }, status: 'awaiting_execution' }));
      if (path === '/api/v1/artifacts') return new Response(JSON.stringify({ artifact_id: 'new-a1', content_hash: 'server-hash' }));
      if (path === '/api/v1/executions/e1/result') {
        const body = JSON.parse(String(init?.body));
        assert.deepEqual(body.events, [{ id: 'event-1', kind: 'tool_result', payload: { name: 'tool' } }]);
        assert.equal(body.trace_coverage, 'partial');
        return new Response(JSON.stringify({ status: 'completed', revision_id: 'r1' }));
      }
      throw new Error(path);
    } });
  const rsi = { turnId: 'turn-1', task: { source: 'external_artifact' as const, artifacts: ['external-output'] } };
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi }), /needs_context/);
  assert.equal(wrapped.cosens.getTurn('turn-1')?.taskId, 't1');
  assert.equal(nativeCalls, 0);
  await wrapped.cosens.atoms!.supplementTaskContext('t1', { expectedContextVersion: 1, objective: 'work', selfContained: true });
  await wrapped.run(new FakeAgent(), 'work', { rsi });
  assert.equal(wrapped.cosens.getTurn('turn-1')?.revisionId, 'r1');
  assert.equal(seen.filter(path => path === '/api/v1/tasks').length, 1);
  assert.equal(nativeCalls, 1);
  await wrapped.close();
});

test('task Runner stream preserves events and submits only at native completion', async () => {
  const seen: string[] = [];
  const nativeStream = {
    finalOutput: 'stream answer', interruptions: [], completed: Promise.resolve(),
    async *[Symbol.asyncIterator]() { yield { type: 'raw_response_event' }; },
  };
  const native = { async run() { return nativeStream; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async (input) => {
    const path = new URL(String(input)).pathname; seen.push(path);
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    if (path === '/api/v1/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path.endsWith('/result')) return new Response(JSON.stringify({ status: 'completed', revision_id: 'r1' }));
    throw new Error(path);
  } });
  const stream = await wrapped.runStreamed(new FakeAgent(), 'work', { rsi: { turnId: 'stream-1', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(seen.includes('/api/v1/artifacts'), false);
  const events = [];
  for await (const event of stream) events.push(event);
  assert.deepEqual(events, [{ type: 'raw_response_event' }]);
  assert.deepEqual(seen.slice(-2), ['/api/v1/artifacts', '/api/v1/executions/e1/result']);
  assert.equal(wrapped.cosens.getTurn('stream-1')?.state, 'completed');
  await wrapped.close();
});

test('task Runner aborted stream cannot submit a completed result', async () => {
  const seen: string[] = [];
  const nativeStream = { finalOutput: 'ignored', interruptions: [], completed: Promise.resolve(), abort() { return 'aborted'; },
    async *[Symbol.asyncIterator]() { yield { type: 'event' }; } };
  const native = { async run() { return nativeStream; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname; seen.push(path);
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  const stream = await wrapped.runStreamed(new FakeAgent(), 'work', { rsi: { turnId: 'cancel-1', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(stream.abort(), 'aborted');
  await stream.completed;
  assert.equal(wrapped.cosens.getTurn('cancel-1')?.state, 'incomplete');
  assert.deepEqual(seen, ['/api/v1/tasks', '/api/v1/tasks/t1/executions']);
  await wrapped.close();
});

test('awaiting a failed native stream records incomplete state and preserves the error', async () => {
  const nativeError = new Error('native stream failed');
  const seen: string[] = [];
  let completion: Promise<void> | undefined;
  const nativeStream = { finalOutput: undefined, interruptions: [],
    get completed() { return completion ??= Promise.reject(nativeError); },
    async *[Symbol.asyncIterator]() { throw nativeError; },
  };
  const native = { async run() { return nativeStream; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname; seen.push(path);
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  try {
    const options = { rsi: { turnId: 'stream-error', task: { source: 'new_task' as const, objective: 'work' } } };
    const stream = await wrapped.runStreamed(new FakeAgent(), 'work', options);
    await assert.rejects(stream.completed, error => error === nativeError);
    assert.equal(wrapped.cosens.getTurn('stream-error')?.state, 'incomplete');
    await assert.rejects(wrapped.runStreamed(new FakeAgent(), 'work', options), /may have executed/);
    assert.deepEqual(seen, ['/api/v1/tasks', '/api/v1/tasks/t1/executions']);
  } finally { await wrapped.close(); }
});

test('task Runner never replays a native execution with unknown side effects', async () => {
  let nativeCalls = 0;
  const native = { async run() { nativeCalls++; throw new Error('native side effect unknown'); } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname;
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  const rsi = { turnId: 'failed-turn', task: { source: 'new_task' as const, objective: 'work' } };
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi }), /native side effect unknown/);
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi }), /may have executed/);
  assert.equal(nativeCalls, 1);
  assert.equal(wrapped.cosens.getTurn('failed-turn')?.state, 'native_failed');
  await wrapped.close();
});

test('task Runner never uploads a fabricated artifact for missing native output', async () => {
  const seen: string[] = [];
  const native = { async run() { return { finalOutput: null, interruptions: [] }; } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname; seen.push(path);
    if (path === '/api/v1/tasks') return new Response(JSON.stringify({ task_id: 't1', status: 'ready' }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  const result = await wrapped.run(new FakeAgent(), 'work', { rsi: { turnId: 'no-output', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(result.finalOutput, null);
  assert.equal(wrapped.cosens.getTurn('no-output')?.state, 'completion_pending');
  assert.deepEqual(seen, ['/api/v1/tasks', '/api/v1/tasks/t1/executions']);
  await wrapped.close();
});

test('task Runner recovery binds original input and Session incarnation', async () => {
  const seen: string[] = [];
  const native = { async run() { throw new Error('must not run'); } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    seen.push(new URL(String(input)).pathname);
    return new Response(JSON.stringify({ task_id: 't1', status: 'needs_context' }));
  } });
  const rsi = { turnId: 'turn-1', task: { source: 'new_task' as const, objective: 'work' } };
  const session1 = { sessionId: 's1', sessionIncarnation: 'first' };
  const session2 = { sessionId: 's1', sessionIncarnation: 'second' };
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi, session: session1 }), /needs_context/);
  await assert.rejects(wrapped.run(new FakeAgent(), 'different input', { rsi, session: session1 }), /input or Session/);
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi, session: session2 }), /input or Session/);
  assert.deepEqual(seen, ['/api/v1/tasks']);
  await wrapped.close();
});
