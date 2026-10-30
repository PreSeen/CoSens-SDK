import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensAgentRunner, cosens } from '../src/index.ts';
import { Agent, MemorySession, tool } from '@openai/agents';
import { ScriptedModel, assistantMessage, functionCall } from '@openai/agents-core/testing';
import { z } from 'zod';

test('explicit runner prepares, ACKs history, calls native once, then submits', async () => {
  const order: string[] = [];
  const sentBatches: string[][] = [];
  const items: any[] = [{ id: 'old', role: 'user', content: 'old' }];
  const session = { getSessionId: async () => 's1', getItems: async () => { order.push('session-read'); return items; } };
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const runner = { run: async (cloned: any, input: string) => {
    order.push('native'); assert.match(cloned.instructions, /CoSens frozen context/);
    assert.equal(input, 'task'); items.push({ id: 'new', role: 'assistant', content: 'done' });
    return { finalOutput: 'done', interruptions: [] };
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; order.push(path);
    if (path.endsWith('/prepare')) { items.push({ id: 'late', role: 'user', content: 'during prepare' }); return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const ids = JSON.parse(String(init?.body)).events.map((e: any) => e.id);
      sentBatches.push(ids);
      return new Response(JSON.stringify({ durable_ack: ids }));
    }
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const statePath = join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3');
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', session, statePath, fetcher, runner });
  const result = await client.run(agent, 'task', { session });
  assert.equal(result.finalOutput, 'done');
  assert.deepEqual(order, ['session-read', '/api/v1/runs/prepare', '/api/v1/runs/r1', '/api/v1/runs/r1/session-events', 'native', 'session-read', '/api/v1/runs/r1/session-events', '/api/v1/runs/r1/submit']);
  assert.deepEqual(sentBatches, [['chat:old'], ['chat:late', 'chat:new']]);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'completed');
  await client.close();
});

test('factory selects the explicit Agent runner', async () => {
  const agent = new Agent({ name: 'helper', instructions: 'Do work' });
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3') });
  assert.ok(client instanceof CoSensAgentRunner);
  await client.close();
});

test('Agent recovery does not reuse a pending Run from an earlier Session incarnation', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const oldSession = { sessionId: 'shared', sessionIncarnation: 'old', getItems: async () => [] };
  const newSession = { sessionId: 'shared', sessionIncarnation: 'new', getItems: async () => [] };
  let prepares = 0, nativeCalls = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: `r${prepares}`, status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ error: { message: 'temporary' } }), { status: 503 });
    if (path.endsWith('/runs/r2') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r2', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher,
    runner: { run: async () => { nativeCalls++; return { finalOutput: 'done', interruptions: [] }; } } });
  await assert.rejects(client.run(agent, 'same task', { session: oldSession }), /temporary/);
  await client.run(agent, 'same task', { session: newSession });
  assert.equal(prepares, 2);
  assert.equal(nativeCalls, 1);
  await client.close();
});

test('Agent pre-execution retry rejects changed input artifacts', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let requests = 0, nativeCalls = 0;
  const fetcher: typeof fetch = async (input) => {
    requests++;
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/artifacts')) return new Response(JSON.stringify({ artifact_id: 'a1', content_hash: 'hash-1' }));
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ error: { message: 'temporary' } }), { status: 503 });
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher,
    runner: { run: async () => { nativeCalls++; return { finalOutput: 'done' }; } } });
  await assert.rejects(client.run(agent, 'task', { inputArtifacts: [{ name: 'source.txt', content: 'original' }] }), /temporary/);
  const before = requests;
  await assert.rejects(client.run(agent, 'task', { inputArtifacts: [{ name: 'source.txt', content: 'changed' }] }), /artifacts/i);
  assert.equal(requests, before);
  assert.equal(nativeCalls, 0);
  await client.close();
});

test('malformed input Artifact receipt stops before prepare and cannot be blindly retried', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let uploads = 0, prepares = 0, nativeCalls = 0;
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/artifacts')) { uploads++; return new Response(JSON.stringify({ artifact_id: 'a1' })); }
    if (path.endsWith('/prepare')) { prepares++; throw Error('prepare must not start'); }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher,
    runner: { run: async () => { nativeCalls++; return { finalOutput: 'done' }; } } });
  await assert.rejects(client.run(agent, 'task', { inputArtifacts: [{ name: 'source.txt', content: 'facts' }] }), /receipt/i);
  await assert.rejects(client.run(agent, 'task', { inputArtifacts: [{ name: 'source.txt', content: 'facts' }] }), /receipt/i);
  assert.deepEqual([uploads, prepares, nativeCalls], [1, 0, 0]);
  await client.close();
});

test('streamed Agent submits only after the native stream finishes', async () => {
  const order: string[] = [];
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const nativeStream = {
    finalOutput: 'done', interruptions: [], completed: Promise.resolve(),
    async *[Symbol.asyncIterator]() { order.push('event'); yield { type: 'text' }; order.push('end'); },
  };
  const runner = { run: async () => nativeStream };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; order.push(path);
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  const stream = await client.runStreamed(agent, 'task');
  assert.equal(order.includes('/api/v1/runs/r1/submit'), false);
  assert.deepEqual([...(await (async () => { const events = []; for await (const e of stream) events.push(e); return events; })())], [{ type: 'text' }]);
  assert.equal(order.at(-1), '/api/v1/runs/r1/submit');
  await client.close();
});

test('streamed Agent holds the Session lane until stream consumption ends', async () => {
  const calls: string[] = [];
  const session = { getSessionId: async () => 's1', getItems: async () => [] };
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const runner = { run: async (_agent: any, input: string) => {
    calls.push(`native:${input}`);
    if (input === 'first') return {
      finalOutput: 'one', interruptions: [], completed: Promise.resolve(),
      async *[Symbol.asyncIterator]() { yield { type: 'text' }; },
    };
    return { finalOutput: 'two', interruptions: [] };
  } };
  let prepares = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; calls.push(`prepare:${prepares}`); return new Response(JSON.stringify({ run_id: `r${prepares}`, status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') || path.endsWith('/runs/r2')) return new Response(JSON.stringify({ run_id: path.slice(-2), status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path + ':' + init?.method);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', session, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  const stream = await client.runStreamed(agent, 'first', { session });
  const second = client.run(agent, 'second', { session });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(calls, ['prepare:1', 'native:first']);
  for await (const _event of stream) { /* consume */ }
  await second;
  assert.deepEqual(calls, ['prepare:1', 'native:first', 'prepare:2', 'native:second']);
  await client.close();
});

test('an explicit Agent retry refuses a different originating mode', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const statePath = join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3');
  const first = new CoSensAgentRunner(agent, {
    apiKey: 'key', mode: 'disable', statePath,
    fetcher: async () => new Response(JSON.stringify({ error: { message: 'temporary' } }), { status: 503 }),
    runner: { run: async () => { throw Error('must not run'); } },
  });
  await assert.rejects(first.run(agent, 'task'), /temporary/);
  await first.close();
  let requests = 0;
  const second = new CoSensAgentRunner(agent, {
    apiKey: 'key', mode: 'auto', statePath,
    fetcher: async () => { requests++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {}, snapshot: {} })); },
    runner: { run: async () => { throw Error('must not run'); } },
  });
  await assert.rejects(second.run(agent, 'task'), /mode/i);
  assert.equal(requests, 0);
  await second.close();
});

test('streamed Agent preserves getters backed by private fields', async () => {
  class NativeStream {
    #output = 'done';
    completed = Promise.resolve();
    get finalOutput() { return this.#output; }
    get interruptions() { return []; }
    async *[Symbol.asyncIterator]() { yield { type: 'text' }; }
  }
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const runner = { run: async () => new NativeStream() };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  const stream = await client.runStreamed(agent, 'task');
  assert.equal(stream.finalOutput, 'done');
  for await (const _event of stream) { /* consume */ }
  await client.close();
});

test('awaiting Agents streamed completed finalizes the Run without async iteration', async () => {
  const order: string[] = [];
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const nativeResult = { finalOutput: 'done', interruptions: [], completed: Promise.resolve(), async *[Symbol.asyncIterator]() { yield { type: 'text' }; } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { order.push('submit'); return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner: { run: async () => nativeResult } });
  const stream = await client.runStreamed(agent, 'task');
  await stream.completed;
  assert.deepEqual(order, ['submit']);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'completed');
  await client.close();
});

test('Agents toStream completes the same Run and cancellation leaves it incomplete', async () => {
  for (const cancelled of [false, true]) {
    let submits = 0;
    const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
    let finish!: () => void;
    const completed = new Promise<void>(resolve => { finish = resolve; });
    const readable = new ReadableStream({ start(controller) { controller.enqueue({ type: 'text' }); controller.close(); } });
    const nativeResult = { finalOutput: 'done', interruptions: [], completed, cancelled: false, toStream() { return readable; }, async *[Symbol.asyncIterator]() { yield { type: 'text' }; } };
    const fetcher: typeof fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
      if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
      if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
      throw Error(path);
    };
    const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner: { run: async () => nativeResult } });
    const stream = await client.runStreamed(agent, 'task');
    const reader = stream.toStream().getReader();
    await reader.read();
    assert.equal(submits, 0);
    nativeResult.cancelled = cancelled;
    finish();
    await stream.completed;
    assert.equal(submits, cancelled ? 0 : 1);
    assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, cancelled ? 'incomplete' : 'completed');
    await client.close();
  }
});

test('zero Session ACK stops before Agent execution and preserves the pending batch', async () => {
  let eventPosts = 0;
  let nativeCalls = 0;
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const session = { sessionId: 's1', getItems: async () => [{ id: 'old', role: 'user', content: 'hello' }] };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      eventPosts++;
      if (eventPosts > 2) throw Error('repeated unacknowledged batch');
      return new Response(JSON.stringify({ durable_ack: [] }));
    }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', session, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner: { run: async () => { nativeCalls++; return { finalOutput: 'done' }; } } });
  await assert.rejects(client.run(agent, 'task', { session }), /acknowledge|ACK/i);
  assert.equal(eventPosts, 1);
  assert.equal(nativeCalls, 0);
  await client.close();
});

test('the pinned Agents Runner receives frozen instructions and returns its native result', async () => {
  const model = new ScriptedModel([[assistantMessage('native answer')]]);
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', model });
  const calls: string[] = [];
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; calls.push(path);
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  const result = await client.run(agent, 'task');
  assert.equal(result.finalOutput, 'native answer');
  assert.equal(model.calls.length, 1);
  assert.match(model.calls[0]!.request.systemInstructions ?? '', /Original instruction/);
  assert.match(model.calls[0]!.request.systemInstructions ?? '', /CoSens frozen context/);
  assert.deepEqual(calls, ['/api/v1/runs/prepare', '/api/v1/runs/r1', '/api/v1/runs/r1/submit']);
  await client.close();
});

test('the pinned Agents tool loop sees one frozen context on every model request', async () => {
  const model = new ScriptedModel([[functionCall('lookup', { query: 'fact' }, { callId: 'call-1' })], [assistantMessage('finished')]]);
  let toolCalls = 0, prepares = 0, submits = 0;
  const lookup = tool({ name: 'lookup', description: 'Look up a fact', parameters: z.object({ query: z.string() }), execute: async ({ query }) => {
    toolCalls++;
    assert.equal(query, 'fact');
    return 'found';
  } });
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', tools: [lookup], model });
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: { summary: 'Frozen recipe' }, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: { memory: 'Frozen memory' } }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  const result = await client.run(agent, 'task');
  assert.equal(result.finalOutput, 'finished');
  assert.deepEqual([model.calls.length, toolCalls, prepares, submits], [2, 1, 1, 1]);
  const instructions = model.calls.map(call => call.request.systemInstructions ?? '');
  assert.equal(instructions[0], instructions[1]);
  assert.equal(instructions[0]!.split('[CoSens frozen context;').length - 1, 1);
  assert.match(instructions[0]!, /Frozen recipe/);
  assert.match(instructions[0]!, /Frozen memory/);
  assert.equal(agent.instructions, 'Original instruction');
  await client.close();
});

test('the pinned Agents MemorySession reports only newly persisted items across two Runs', async () => {
  const model = new ScriptedModel([[assistantMessage('first answer')], [assistantMessage('second answer')]]);
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', model });
  const session = new MemorySession({ sessionId: 'native-session-1', initialItems: [{ role: 'user', content: 'old request' }] });
  const batches: Array<{ runId: string; ids: string[] }> = [];
  let prepares = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: `r${prepares}`, status: 'ready', recipe: {}, context: {} })); }
    if (/\/runs\/r\d+$/.test(path) && init?.method === 'GET') return new Response(JSON.stringify({ run_id: path.split('/').at(-1), status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const ids = JSON.parse(String(init?.body)).events.map((item: any) => item.id);
      batches.push({ runId: path.split('/').at(-2)!, ids });
      return new Response(JSON.stringify({ durable_ack: ids }));
    }
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', session, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  assert.equal((await client.run(agent, 'first task', { session })).finalOutput, 'first answer');
  assert.equal((await client.run(agent, 'second task', { session })).finalOutput, 'second answer');
  assert.equal(model.calls.length, 2);
  assert.equal(prepares, 2);
  assert.equal(batches[0]?.runId, 'r1');
  assert.equal(batches[0]?.ids.length, 1);
  assert.ok(batches[0]!.ids[0]!.startsWith('chat:'));
  const firstRunIds = new Set(batches.filter(batch => batch.runId === 'r1').flatMap(batch => batch.ids));
  const secondRunIds = batches.filter(batch => batch.runId === 'r2').flatMap(batch => batch.ids);
  assert.ok(firstRunIds.size >= 3);
  assert.ok(secondRunIds.length >= 2);
  assert.ok(secondRunIds.every(id => !firstRunIds.has(id)));
  await client.close();
});

test('the pinned Agents streamed result completes and submits when only completed is awaited', async () => {
  const model = new ScriptedModel([[assistantMessage('streamed answer')]]);
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', model });
  let submits = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  const streamed = await client.runStreamed(agent, 'task');
  await streamed.completed;
  assert.equal(streamed.finalOutput, 'streamed answer');
  assert.equal(submits, 1);
  await client.close();
});

test('native approval resume keeps the original CoSens Run and frozen context', async () => {
  const calls: string[] = [];
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const session = { sessionId: 'same', sessionIncarnation: 'old', getItems: async () => [] };
  const approvedState = { approved: true };
  const runner = { run: async (cloned: any, input: any) => {
    calls.push(`native:${typeof input === 'string' ? input : 'resume'}`);
    assert.match(cloned.instructions, /RSI Run r1/);
    if (typeof input === 'string') return { finalOutput: undefined, interruptions: [{ type: 'tool_approval' }], state: { toString: () => 'serialized-state' } };
    assert.equal(input, approvedState);
    return { finalOutput: 'approved result', interruptions: [] };
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; calls.push(path);
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  await client.run(agent, 'task', { session });
  const turnId = client.cosens.lastTurnId!;
  assert.equal(client.cosens.getTurn(turnId)?.state, 'awaiting_native_approval');
  await assert.rejects(client.resume(agent, turnId, approvedState, { session: { ...session, sessionIncarnation: 'new' } }), /Session incarnation/);
  const result = await client.resume(agent, turnId, approvedState, { session });
  assert.equal(result.finalOutput, 'approved result');
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completed');
  assert.deepEqual(calls, ['/api/v1/runs/prepare', '/api/v1/runs/r1', 'native:task', 'native:resume', '/api/v1/runs/r1/submit']);
  await client.close();
});

test('the pinned Agents tool approval state round-trips through pendingState and resume', async () => {
  const model = new ScriptedModel([[functionCall('lookup', { query: 'fact' }, { callId: 'call-1' })], [assistantMessage('approved answer')]]);
  let toolCalls = 0, prepares = 0, submits = 0;
  const lookup = tool({ name: 'lookup', description: 'Look up a fact', parameters: z.object({ query: z.string() }), needsApproval: true,
    execute: async ({ query }) => { toolCalls++; return `found ${query}`; } });
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', tools: [lookup], model });
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: { summary: 'Frozen recipe' }, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: { memory: 'Frozen memory' } }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  const interrupted = await client.run(agent, 'task');
  const turnId = client.cosens.lastTurnId!;
  assert.equal(interrupted.interruptions.length, 1);
  assert.equal(client.cosens.getTurn(turnId)?.state, 'awaiting_native_approval');
  assert.deepEqual([model.calls.length, toolCalls, prepares, submits], [1, 0, 1, 0]);
  const pending = await client.pendingState(agent, turnId);
  pending.approve(pending.getInterruptions()[0]);
  const resumed = await client.resume(agent, turnId, pending);
  assert.equal(resumed.finalOutput, 'approved answer');
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completed');
  assert.deepEqual([model.calls.length, toolCalls, prepares, submits], [2, 1, 1, 1]);
  assert.equal((model.calls[0]!.request.systemInstructions ?? '').split('[CoSens frozen context;').length - 1, 1);
  assert.equal((model.calls[1]!.request.systemInstructions ?? '').split('[CoSens frozen context;').length - 1, 1);
  await client.close();
});

test('the pinned streamed Agents approval resumes and submits its original Run', async () => {
  const model = new ScriptedModel([[functionCall('lookup', { query: 'fact' }, { callId: 'call-1' })], [assistantMessage('streamed approved answer')]]);
  let toolCalls = 0, prepares = 0, submits = 0;
  const lookup = tool({ name: 'lookup', description: 'Look up a fact', parameters: z.object({ query: z.string() }), needsApproval: true,
    execute: async () => { toolCalls++; return 'found'; } });
  const agent = new Agent({ name: 'helper', instructions: 'Original instruction', tools: [lookup], model });
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher });
  const interrupted = await client.runStreamed(agent, 'task');
  await interrupted.completed;
  const turnId = client.cosens.lastTurnId!;
  assert.equal(client.cosens.getTurn(turnId)?.state, 'awaiting_native_approval');
  assert.deepEqual([prepares, submits, toolCalls], [1, 0, 0]);
  const pending = await client.pendingState(agent, turnId);
  pending.approve(pending.getInterruptions()[0]);
  const resumed = await client.resume(agent, turnId, pending, { stream: true });
  await resumed.completed;
  assert.equal(resumed.finalOutput, 'streamed approved answer');
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completed');
  assert.deepEqual([model.calls.length, prepares, submits, toolCalls], [2, 1, 1, 1]);
  await client.close();
});

test('native approval resumed as a stream submits only after its completion', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let finish!: () => void;
  const completed = new Promise<void>(resolve => { finish = resolve; });
  const streamed = { finalOutput: 'approved result', interruptions: [], completed, async *[Symbol.asyncIterator]() { yield { type: 'text' }; } };
  let submits = 0, prepares = 0;
  const runner = { run: async (_agent: any, input: any) => typeof input === 'string'
    ? { finalOutput: undefined, interruptions: [{ type: 'tool_approval' }], state: { toString: () => 'serialized-state' } }
    : streamed };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  await client.run(agent, 'task');
  const turnId = client.cosens.lastTurnId!;
  const result = await client.resume(agent, turnId, { approved: true }, { stream: true });
  assert.equal(submits, 0);
  finish();
  await result.completed;
  assert.equal(submits, 1);
  assert.equal(prepares, 1);
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completed');
  await client.close();
});

test('two Agent runner instances serialize one Session through shared SQLite state', async () => {
  const calls: string[] = [];
  const session = { sessionId: 'shared', getItems: async () => [] };
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>(resolve => { releaseFirst = resolve; });
  const runner = { run: async (_agent: any, input: string) => {
    calls.push(`native:${input}`);
    if (input === 'first') await firstGate;
    return { finalOutput: input, interruptions: [] };
  } };
  let prepareCount = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepareCount++; calls.push(`prepare:${prepareCount}`); return new Response(JSON.stringify({ run_id: `r${prepareCount}`, status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') || path.endsWith('/runs/r2')) return new Response(JSON.stringify({ run_id: path.slice(-2), status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path + ':' + init?.method);
  };
  const statePath = join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3');
  const options = { apiKey: 'key', mode: 'disable' as const, session, statePath, fetcher, runner };
  const firstClient = new CoSensAgentRunner(agent, options);
  const secondClient = new CoSensAgentRunner(agent, options);
  const first = firstClient.run(agent, 'first', { session });
  while (!calls.includes('native:first')) await new Promise(resolve => setTimeout(resolve, 1));
  const second = secondClient.run(agent, 'second', { session });
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(calls, ['prepare:1', 'native:first']);
  releaseFirst();
  await Promise.all([first, second]);
  assert.deepEqual(calls, ['prepare:1', 'native:first', 'prepare:2', 'native:second']);
  await firstClient.close(); await secondClient.close();
});

test('Agent reconcile after a lost submit receipt never reruns native work', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const statePath = join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3');
  let nativeCalls = 0, prepares = 0, submits = 0;
  const runner = { run: async () => { nativeCalls++; return { finalOutput: 'real result', interruptions: [] }; } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify(submits
      ? { run_id: 'r1', status: 'completed', result: { outcome: 'completed', summary: 'real result' } }
      : { run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return submits === 1
      ? new Response(JSON.stringify({ error: { message: 'temporary' } }), { status: 503 })
      : new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const options = { apiKey: 'key', mode: 'disable' as const, statePath, fetcher, runner };
  const first = new CoSensAgentRunner(agent, options);
  await first.run(agent, 'task');
  const turnId = first.cosens.lastTurnId!;
  assert.equal(first.cosens.getTurn(turnId)?.state, 'completion_pending');
  await first.close();
  const restarted = new CoSensAgentRunner(agent, options);
  const recovered = await restarted.cosens.reconcile(turnId);
  assert.equal(recovered.state, 'completed');
  assert.deepEqual([nativeCalls, prepares, submits], [1, 1, 1]);
  await restarted.close();
});

test('Agent uploads real input and output text artifacts before prepare and submit', async () => {
  const order: string[] = [];
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  const runner = { run: async () => { order.push('native'); return { finalOutput: { summary: 'done', files: [{ name: 'answer.txt', content: 'answer' }] }, interruptions: [] }; } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/artifacts')) {
      const body = JSON.parse(String(init?.body)); order.push(`artifact:${body.name}`);
      return new Response(JSON.stringify({ artifact_id: body.name === 'input.txt' ? 'a-in' : 'a-out', content_hash: 'hash' }));
    }
    if (path.endsWith('/prepare')) {
      order.push('prepare'); assert.deepEqual(JSON.parse(String(init?.body)).input_artifact_ids, ['a-in']);
      return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) {
      order.push('submit');
      assert.deepEqual(JSON.parse(String(init?.body)).data.files, [{ artifact_id: 'a-out', content_hash: 'hash' }]);
      return new Response(JSON.stringify({ status: 'completed' }));
    }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher, runner });
  await client.run(agent, 'task', { inputArtifacts: [{ name: 'input.txt', content: 'source' }] });
  assert.deepEqual(order, ['artifact:input.txt', 'prepare', 'native', 'artifact:answer.txt', 'submit']);
  await client.close();
});

test('malformed output Artifact receipt keeps the completed native result pending without re-upload', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let uploads = 0, submits = 0, nativeCalls = 0, getRuns = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') { getRuns++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} })); }
    if (path.endsWith('/artifacts')) { uploads++; return new Response(JSON.stringify({ artifact_id: 'a1' })); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher,
    runner: { run: async () => { nativeCalls++; return { finalOutput: { summary: 'done', files: [{ name: 'out.txt', content: 'answer' }] }, interruptions: [] }; } } });
  await client.run(agent, 'task');
  const turnId = client.cosens.lastTurnId!;
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completion_pending');
  await assert.rejects(client.cosens.reconcile(turnId), /receipt/i);
  assert.deepEqual([nativeCalls, uploads, submits, getRuns], [1, 1, 0, 2]);
  await client.close();
});

test('malformed native output files stay pending and never silently submit', async () => {
  const agent = { name: 'helper', instructions: 'Original', clone(config: any) { return { ...this, ...config }; } };
  let uploads = 0, submits = 0, nativeCalls = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/artifacts')) { uploads++; throw Error('invalid file must not upload'); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = new CoSensAgentRunner(agent, { apiKey: 'key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-agent-')), 'state.sqlite3'), fetcher,
    runner: { run: async () => { nativeCalls++; return { finalOutput: { summary: 'done', files: 'bad-shape' }, interruptions: [] }; } } });
  await client.run(agent, 'task');
  const turnId = client.cosens.lastTurnId!;
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completion_pending');
  await assert.rejects(client.cosens.reconcile(turnId), /files must be an array/i);
  assert.deepEqual([nativeCalls, uploads, submits], [1, 0, 0]);
  await client.close();
});
