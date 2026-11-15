import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Agent, Runner, tool } from '@openai/agents';
import { ScriptedModel, assistantMessage, functionCall } from '@openai/agents-core/testing';
import { z } from 'zod';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cosens, RSIApprovalUnavailable, RSIHTTPError, RSIRecoveryRequired, TaskWorkflowRunner } from '../src/index.ts';

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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    if (path === '/api/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path.endsWith('/result')) return new Response(JSON.stringify({ status: 'completed', trace_origin: 'original' }));
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
    for (const path of ['/api/tasks', '/api/executions', '/api/executions/e1/result']) assert.equal(seen.filter(x => x === path).length, 1);
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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path === '/api/executions') return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'frozen-hash', request: { kind: 'original' } }));
    if (path === '/api/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path === '/api/executions/e1/result') {
      submissions++;
      return submissions === 1 ? new Response(JSON.stringify({ error: { code: 'transient', message: 'receipt lost' } }), { status: 503 }) :
        new Response(JSON.stringify({ status: 'completed', trace_origin: 'original' }));
    }
    throw new Error(path);
  } });
  const rsi = { turnId: 'turn-1', task: { source: 'new_task' as const, objective: 'work' } };
  const result = await wrapped.run(new FakeAgent(), 'work', { rsi });
  assert.equal(result.finalOutput, 'native answer');
  assert.equal(wrapped.cosens.getTurn('turn-1')?.state, 'completion_pending');
  await assert.rejects(wrapped.run(new FakeAgent(), 'work', { rsi }), /may have executed/);
  const recovered = await wrapped.reconcile('turn-1');
  assert.equal(recovered.state, 'completed'); assert.equal('revisionId' in recovered, false);
  assert.equal(nativeCalls, 1);
  assert.equal(seen.filter(x => x.path === '/api/artifacts').length, 1);
  assert.deepEqual(seen.slice(0, 2), [{ path: '/api/tasks', body: { objective: 'work', artifacts: [] } },
    { path: '/api/executions', body: { task_id: 't1', source: 'new_task', input: { objective: 'work', artifacts: [] } } }]);
  const bodies = seen.filter(x => x.path.endsWith('/result')).map(x => x.body);
  assert.deepEqual(bodies[0], bodies[1]);
  assert.deepEqual(bodies[0], { request_sha256: 'frozen-hash', outcome: 'completed', artifacts: [{ artifact_id: 'a1', role: 'submitted_output' }],
    events: [{ id: 'native-item-1', kind: 'tool_call_output_item', payload: { item_type: 'tool_call_output_item', raw_item: { output: 'observed tool output' } } }], trace_coverage: 'partial' });
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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    if (path === '/api/artifacts') return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'server-hash' }));
    if (path.endsWith('/result')) return new Response(JSON.stringify({ status: 'completed', trace_origin: 'original' }));
    throw new Error(path);
  } });
  const stream = await wrapped.runStreamed(new FakeAgent(), 'work', { rsi: { turnId: 'stream-1', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(seen.includes('/api/artifacts'), false);
  const events = [];
  for await (const event of stream) events.push(event);
  assert.deepEqual(events, [{ type: 'raw_response_event' }]);
  assert.deepEqual(seen.slice(-2), ['/api/artifacts', '/api/executions/e1/result']);
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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  const stream = await wrapped.runStreamed(new FakeAgent(), 'work', { rsi: { turnId: 'cancel-1', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(stream.abort(), 'aborted');
  await stream.completed;
  assert.equal(wrapped.cosens.getTurn('cancel-1')?.state, 'incomplete');
  assert.deepEqual(seen, ['/api/tasks', '/api/executions']);
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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  try {
    const options = { rsi: { turnId: 'stream-error', task: { source: 'new_task' as const, objective: 'work' } } };
    const stream = await wrapped.runStreamed(new FakeAgent(), 'work', options);
    await assert.rejects(stream.completed, error => error === nativeError);
    assert.equal(wrapped.cosens.getTurn('stream-error')?.state, 'incomplete');
    await assert.rejects(wrapped.runStreamed(new FakeAgent(), 'work', options), /may have executed/);
    assert.deepEqual(seen, ['/api/tasks', '/api/executions']);
  } finally { await wrapped.close(); }
});

test('task Runner never replays a native execution with unknown side effects', async () => {
  let nativeCalls = 0;
  const native = { async run() { nativeCalls++; throw new Error('native side effect unknown'); } } as unknown as Runner;
  const wrapped = new TaskWorkflowRunner(native, { mode: 'auto', apiKey: 'key', statePath: ':memory:', fetch: async input => {
    const path = new URL(String(input)).pathname;
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
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
    if (path === '/api/tasks') return new Response(JSON.stringify({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 }));
    if (path.endsWith('/executions')) return new Response(JSON.stringify({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'original' } }));
    throw new Error(path);
  } });
  const result = await wrapped.run(new FakeAgent(), 'work', { rsi: { turnId: 'no-output', task: { source: 'new_task', objective: 'work' } } });
  assert.equal(result.finalOutput, null);
  assert.equal(wrapped.cosens.getTurn('no-output')?.state, 'completion_pending');
  assert.deepEqual(seen, ['/api/tasks', '/api/executions']);
  await wrapped.close();
});


/**
 * Refuses POST /executions as input_incomplete while `incomplete` holds, naming what the input lacks as the
 * service does, or answers `executionError` instead; one task body per idempotency key.
 */
class IncompleteServer {
  readonly seen: Array<[string, string, any]> = [];
  readonly keys: Record<string, Array<string | null>> = {};
  private readonly taskBodies = new Map<string | null, string>();
  incomplete = true;
  executionError?: () => Response;

  static missing(body: any): string[] {
    const roles = body.input.artifacts.map((a: any) => typeof a === 'string' ? (body.source === 'new_task' ? 'original_input' : 'submitted_output') : a.role);
    const missing = body.input.objective ? [] : ['objective'];
    if (body.source === 'external_artifact') {
      if (!roles.includes('original_input') && !body.input.self_contained) missing.push('original_input_or_self_contained_task');
      if (!roles.includes('submitted_output')) missing.push('submitted_output');
    }
    return missing;
  }

  readonly fetch: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
    const key = new Headers(init?.headers).get('idempotency-key');
    this.seen.push([init?.method ?? 'GET', path, body]);
    (this.keys[path] ??= []).push(key);
    const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
    if (path === '/api/tasks') {
      if (!this.taskBodies.has(key)) this.taskBodies.set(key, JSON.stringify(body));
      if (this.taskBodies.get(key) !== JSON.stringify(body)) return json({ error: { code: 'invalid_request', message: 'idempotency key reused with different task' } }, 422);
      return json({ task_id: 't1', objective: body.objective, artifacts: [], context_version: 1 });
    }
    if (path === '/api/tasks/t1') return json({ task_id: 't1', objective: 'work', artifacts: [], context_version: 1 });
    if (path === '/api/executions') {
      if (this.executionError) return this.executionError();
      if (this.incomplete) return json({ error: { code: 'input_incomplete', message: 'the execution input is incomplete', missing: IncompleteServer.missing(body) } }, 422);
      return json({ execution_id: 'e1', status: 'awaiting_execution', request_sha256: 'h1', request: { kind: 'reconstructed' } });
    }
    if (path === '/api/artifacts') return json({ artifact_id: 'a1', content_hash: 'server-hash' });
    if (path === '/api/executions/e1/result') return json({ status: 'completed', trace_origin: 'reconstructed' });
    throw new Error(path);
  };
}

function incompleteRunner(mode: 'auto' | 'approval' = 'auto', statePath = ':memory:') {
  const server = new IncompleteServer();
  const calls: unknown[] = [];
  const native = { async run(...args: unknown[]) { calls.push(args); return { finalOutput: 'native answer', interruptions: [] }; } } as unknown as Runner;
  const wrapper = new TaskWorkflowRunner(native, { mode, apiKey: 'key', statePath, fetch: server.fetch });
  return { server, calls, wrapper };
}
const INCOMPLETE = { source: 'external_artifact' as const, objective: 'work', artifacts: ['external-output'] };

test('task Runner raises incomplete input unchanged and binds the retry', async () => {
  const { server, calls, wrapper } = incompleteRunner();
  const rsi = { turnId: 'turn-1', task: INCOMPLETE };
  const session1 = { sessionId: 's1', sessionIncarnation: 'first' };
  const error: any = await wrapper.run(new FakeAgent(), 'work', { rsi, session: session1 }).then(() => undefined, value => value);
  assert.ok(error instanceof RSIHTTPError);
  assert.deepEqual([error.statusCode, error.code, error.missing, error.recipeIds], [422, 'input_incomplete', ['original_input_or_self_contained_task'], undefined]);
  assert.deepEqual(calls, []); assert.equal(wrapper.cosens.getTurn('turn-1')?.state, 'pre_execution_pending');
  for (const [input, session] of [['different input', session1], ['work', { sessionId: 's1', sessionIncarnation: 'second' }]] as const) {
    await assert.rejects(wrapper.run(new FakeAgent(), input, { rsi, session }), /input or Session/);
  }
  assert.deepEqual(server.seen.map(entry => entry[1]), ['/api/tasks', '/api/executions']);
  server.incomplete = false;
  const result = await wrapper.run(new FakeAgent(), 'work', { rsi, session: session1 });
  assert.equal(result.finalOutput, 'native answer'); assert.equal(calls.length, 1);
  // The retry runs the same spec as the execution input of the same task, under the same keys.
  const execution = { task_id: 't1', source: 'external_artifact', input: { objective: 'work', artifacts: ['external-output'] } };
  assert.deepEqual(server.seen[0]![2], { objective: 'work', artifacts: [] });
  assert.deepEqual(server.seen.filter(entry => entry[1] === '/api/executions').map(entry => entry[2]), [execution, execution]);
  assert.equal(new Set(server.keys['/api/executions']).size, 1);
  // The task is asked for again under the same key.
  assert.equal(server.keys['/api/tasks']!.length, 2); assert.equal(new Set(server.keys['/api/tasks']).size, 1);
  await wrapper.close();
});

test('task Runner retries an incomplete turn with a corrected spec', async () => {
  const { server, calls, wrapper } = incompleteRunner();
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }), /incomplete/);
  server.incomplete = false;
  const corrected = { ...INCOMPLETE, self_contained: true };
  const result = await wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: corrected } });
  assert.equal(result.finalOutput, 'native answer'); assert.equal(calls.length, 1);
  const turn: any = wrapper.cosens.getTurn('turn-1');
  assert.deepEqual([turn.executionId, turn.taskSpec, turn.taskId, 'taskSpecIncomplete' in turn], ['e1', corrected, 't1', false]);
  assert.deepEqual(server.seen.filter(entry => entry[1] === '/api/executions').at(-1)![2].input, { objective: 'work', artifacts: ['external-output'], self_contained: true });
  // Once the turn has started an execution its spec is fixed again.
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }), /may have executed/);
  await wrapper.close();
});

test('task Runner keeps the objective when correcting an incomplete spec', async () => {
  const { server, calls, wrapper } = incompleteRunner();
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }), /incomplete/);
  const sent = server.seen.length;
  // The task was created and keyed from the objective, so a correction may complete only the execution input.
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: { ...INCOMPLETE, objective: 'other work', self_contained: true } } }),
    /objective changed/);
  assert.equal(server.seen.length, sent); assert.deepEqual(wrapper.cosens.getTurn('turn-1')?.taskSpec, INCOMPLETE);
  server.incomplete = false;
  const corrected = { ...INCOMPLETE, artifacts: ['external-output', { artifact_id: 'brief', role: 'original_input' as const }] };
  await wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: corrected } });
  assert.equal(wrapper.cosens.getTurn('turn-1')?.executionId, 'e1'); assert.equal(calls.length, 1);
  assert.deepEqual([...new Set(server.seen.filter(entry => entry[1] === '/api/tasks').map(entry => JSON.stringify(entry[2])))], [JSON.stringify({ objective: 'work', artifacts: [] })]);
  await wrapper.close();
});

test('task Runner keeps a spec the host refused correctable', async () => {
  const { server, wrapper } = incompleteRunner('approval');
  const answers = [true, false, true]; const asked: unknown[] = [];
  const authorize = (_phase: string, snapshot: any) => { asked.push(snapshot.task); return answers[asked.length - 1]!; };
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE, authorize } }), /incomplete/);
  const sent = server.seen.length;
  // The host refuses the first correction before anything is sent, so the turn may still be corrected.
  const refused = { ...INCOMPLETE, self_contained: true };
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: refused, authorize } }), RSIApprovalUnavailable);
  let turn: any = wrapper.cosens.getTurn('turn-1');
  assert.equal(server.seen.length, sent); assert.deepEqual(turn.taskSpec, refused); assert.equal(turn.taskSpecIncomplete, true);
  server.incomplete = false;
  const corrected = { ...INCOMPLETE, artifacts: ['external-output', { artifact_id: 'brief', role: 'original_input' as const }] };
  const result = await wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: corrected, authorize } });
  assert.equal(result.finalOutput, 'native answer'); assert.deepEqual(asked, [INCOMPLETE, refused, corrected]);
  turn = wrapper.cosens.getTurn('turn-1');
  assert.deepEqual([turn.executionId, turn.taskSpec, 'taskSpecIncomplete' in turn], ['e1', corrected, false]);
  assert.deepEqual(server.seen.filter(entry => entry[1] === '/api/executions').at(-1)![2].input,
    { objective: 'work', artifacts: ['external-output', { artifact_id: 'brief', role: 'original_input' }] });
  await wrapper.close();
});

test('task Runner checks the model before asking the host', async () => {
  const { server, wrapper } = incompleteRunner('approval');
  const agent: any = new FakeAgent(); agent.model = 'm'.repeat(300);
  const asked: unknown[] = [];
  await assert.rejects(wrapper.run(agent, 'work', { rsi: { turnId: 'turn-1', task: { source: 'new_task', objective: 'work' },
    authorize: (...args) => { asked.push(args); return true; } } }), /model must be a public model ID/);
  assert.deepEqual(asked, []); assert.deepEqual(server.seen, []); assert.equal(wrapper.cosens.getTurn('turn-1'), undefined);
  await wrapper.close();
});

for (const [name, failure] of [
  ['refused', () => new Response(JSON.stringify({ error: { code: 'invalid_request', message: 'bad execution' } }), { status: 422 })],
  ['server_error', () => new Response(JSON.stringify({ error: { code: 'input_incomplete', message: 'unavailable' } }), { status: 503 })],
  ['started_not_ready', () => new Response(JSON.stringify({ execution_id: 'e1', status: 'failed', request_sha256: 'h1', request: {} }))],
  ['receipt_lost', () => { throw new Error('receipt lost'); }],
] as const) {
  test(`task Runner locks the spec after a start that was not input_incomplete: ${name}`, async () => {
    const { server, calls, wrapper } = incompleteRunner();
    await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }), /incomplete/);
    // The next start of the same spec fails some other way, so it may have started an execution.
    server.executionError = failure;
    await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }));
    assert.equal('taskSpecIncomplete' in wrapper.cosens.getTurn('turn-1')!, false);
    const sent = server.seen.length;
    await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: { ...INCOMPLETE, self_contained: true } } }), /Workflow task changed/);
    assert.equal(server.seen.length, sent); assert.deepEqual(calls, []);
    await wrapper.close();
  });
}

test('task Runner refuses a corrected spec from another mode without recording it', async () => {
  const path = join(mkdtempSync(join(tmpdir(), 'cosens-runner-mode-')), 'state.sqlite3');
  let { wrapper } = incompleteRunner('auto', path);
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: INCOMPLETE } }), /incomplete/);
  await wrapper.close();
  ({ wrapper } = incompleteRunner('approval', path));
  await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: { ...INCOMPLETE, self_contained: true }, authorize: () => true } }),
    (error: unknown) => error instanceof RSIRecoveryRequired && /different mode/.test(error.message));
  await wrapper.close();
  ({ wrapper } = incompleteRunner('auto', path));
  const turn: any = wrapper.cosens.getTurn('turn-1');
  assert.deepEqual(turn.taskSpec, INCOMPLETE); assert.equal(turn.taskSpecIncomplete, true);
  await wrapper.close();
});

for (const [spec, message] of [
  [{ objective: 'work' }, /task.source is required/],
  [{ source: 'external_artifact', objective: 'work' }, /external_artifact requires submitted_output/],
  [{ source: 'new_task' }, /objective must be nonempty/],
] as const) {
  test(`task Runner validates the spec before creating a task: ${message.source}`, async () => {
    const { server, calls, wrapper } = incompleteRunner();
    await assert.rejects(wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: spec as any } }), message);
    assert.deepEqual(server.seen, []); assert.deepEqual(calls, []); assert.equal(wrapper.cosens.getTurn('turn-1'), undefined);
    // Nothing was recorded, so the same turn runs once the spec is fixed.
    server.incomplete = false;
    await wrapper.run(new FakeAgent(), 'work', { rsi: { turnId: 'turn-1', task: { source: 'new_task', objective: 'work', self_contained: true } } });
    assert.equal(wrapper.cosens.getTurn('turn-1')?.executionId, 'e1');
    await wrapper.close();
  });
}
