import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { cosens } from '../src/index.ts';

const statePath = () => join(mkdtempSync(join(tmpdir(), 'cosens-proxy-')), 'state.sqlite3');

test('disable recalls before one native model call and preserves unrelated resources', async () => {
  const order: string[] = [];
  const native = {
    responses: { create: async (params: any) => { order.push('model'); assert.match(params.instructions, /CoSens frozen context/); return { id: 'resp-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] }; } },
    models: { list: async () => ['m1'] },
  };
  const fetcher: typeof fetch = async (input) => {
    const path = new URL(String(input)).pathname;
    order.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }), { status: 200 });
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const response = await client.responses.create({ model: 'model', input: 'hello' });
  assert.equal(response.output_text, 'done');
  assert.deepEqual(order, ['/api/v1/recipes/active', '/api/v1/memory/recall', 'model']);
  assert.deepEqual(await client.models.list(), ['m1']);
  await client.close();
});

test('auto active prepares and freezes before native call, then submits once', async () => {
  const order: string[] = [];
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) { order.push('decision'); return { output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) }; }
    order.push('model'); assert.match(params.instructions, /RSI Run run-1/);
    return { id: 'resp-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; order.push(path);
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'completed' }));
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const response = await client.responses.create({ model: 'model', input: 'hello' });
  assert.equal(response.output_text, 'done');
  assert.deepEqual(order, ['decision', '/api/v1/runs/prepare', '/api/v1/runs/run-1', 'model', '/api/v1/runs/run-1/session-events', '/api/v1/runs/run-1/submit']);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'completed');
  await client.close();
});

test('raw response access starts one native request without parsing or submitting it', async () => {
  let modelCalls = 0;
  const native = { responses: { create: (_params: any) => {
    modelCalls++;
    const promise = Promise.resolve({ status: 'completed', output_text: 'done', output: [{ type: 'message' }] });
    return Object.assign(promise, { asResponse: async () => new Response('raw', { headers: { 'x-test': 'ok' } }), withResponse: async () => ({ data: await promise, response: new Response('raw') }) });
  } } };
  const paths: string[] = [];
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname; paths.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const raw = await client.responses.create({ model: 'model', input: 'hello' }).asResponse();
  assert.equal(raw.headers.get('x-test'), 'ok');
  assert.equal(modelCalls, 1);
  assert.equal(paths.length, 2);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('generation request options survive the CoSens preflight', async () => {
  const calls: string[] = [];
  const native = { responses: { create: async (_body: any, requestOptions: any) => {
    calls.push('model');
    assert.equal(requestOptions.headers['x-native-option'], 'kept');
    return { id: 'r1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    calls.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  await client.responses.create({ model: 'model', input: 'hello' }, { headers: { 'x-native-option': 'kept' } });
  assert.deepEqual(calls, ['/api/v1/recipes/active', '/api/v1/memory/recall', 'model']);
  await client.close();
});

test('a caller AbortSignal during recall prevents the native generation request', async () => {
  const controller = new AbortController();
  let releaseRecall!: () => void;
  let enteredRecall!: () => void;
  const recallStarted = new Promise<void>(resolve => { enteredRecall = resolve; });
  const recallGate = new Promise<void>(resolve => { releaseRecall = resolve; });
  let nativeCalls = 0;
  const native = { responses: { create: async () => { nativeCalls++; return { status: 'completed', output_text: 'wrong', output: [{ type: 'message' }] }; } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/recipes/active')) { enteredRecall(); await recallGate; return new Response(JSON.stringify({ status: 'not_found' })); }
    if (path.endsWith('/memory/recall')) return new Response(JSON.stringify({ items: [] }));
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const pending = Promise.resolve(client.responses.create({ model: 'model', input: 'hello' }, { signal: controller.signal }));
  await recallStarted;
  controller.abort();
  releaseRecall();
  await assert.rejects(pending, error => error instanceof DOMException && error.name === 'AbortError');
  assert.equal(nativeCalls, 0);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('a caller AbortSignal after a raw stream starts marks the unconsumed turn incomplete', async () => {
  const controller = new AbortController();
  let modelCalls = 0, submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return { async *[Symbol.asyncIterator]() { yield { type: 'response.created' }; } };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = await client.responses.create({ model: 'model', input: 'hello', stream: true }, { signal: controller.signal });
  assert.equal(typeof stream[Symbol.asyncIterator], 'function');
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'native_running');
  controller.abort();
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.deepEqual([modelCalls, submits], [1, 0]);
  await client.close();
});

test('responses.stream returns an event stream synchronously and submits after its terminal event', async () => {
  const calls: string[] = [];
  const response = { id: 'r1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  const nativeStream = {
    listeners: new Map<string, Function[]>(),
    on(name: string, listener: Function) { this.listeners.set(name, [...this.listeners.get(name) ?? [], listener]); return this; },
    async *[Symbol.asyncIterator]() {
      const event = { type: 'response.completed', response };
      for (const callback of this.listeners.get('response.completed') ?? []) callback(event);
      yield event;
    },
    async finalResponse() { return response; },
    abort() {},
  };
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }),
    stream: (_body: any) => { calls.push('native-stream'); return nativeStream; },
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname; calls.push(path);
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((event: any) => event.id) }));
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw new Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  assert.equal(typeof stream.on, 'function');
  assert.equal(typeof stream.finalResponse, 'function');
  assert.equal(typeof stream[Symbol.asyncIterator], 'function');
  let notified = false;
  stream.on('response.completed', () => { notified = true; });
  const events = [];
  for await (const event of stream) events.push(event);
  assert.equal(events[0].type, 'response.completed');
  assert.equal(notified, true);
  assert.equal((await stream.finalResponse()).id, 'r1');
  assert.equal(calls.at(-1), '/api/v1/runs/run-1/submit');
  await client.close();
});

test('Responses stream done waits for CoSens terminal submission', async () => {
  let finish!: () => void;
  const gate = new Promise<void>(resolve => { finish = resolve; });
  let submits = 0;
  const response = { id: 'resp-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  const nativeStream = {
    on() { return this; },
    async done() {},
    async finalResponse() { await gate; return response; },
    async *[Symbol.asyncIterator]() { yield { type: 'response.completed', response }; },
  };
  const native = { responses: {
    create: async (params: any) => {
      if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
      throw Error('unexpected model create');
    },
    stream: () => nativeStream,
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  const done = stream.done();
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(submits, 0);
  finish();
  await done;
  assert.equal(submits, 1);
  await client.close();
});

test('Responses stream done preserves a native final-response failure', async () => {
  let submits = 0;
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) }),
    stream: () => ({
      on() { return this; },
      async done() {},
      async finalResponse() { throw new Error('native final response failed'); },
      async *[Symbol.asyncIterator]() {},
    }),
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await assert.rejects(stream.done(), /native final response failed/);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.equal(submits, 0);
  await client.close();
});

test('Responses stream done failure marks an unconsumed turn incomplete', async () => {
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) }),
    stream: () => ({
      on() { return this; },
      async done() { throw new Error('native transport disconnected'); },
      finalResponse: () => new Promise(() => {}),
      async *[Symbol.asyncIterator]() {},
    }),
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await assert.rejects(stream.done(), /native transport disconnected/);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('Responses event iteration propagates a native final-response failure', async () => {
  const native = { responses: { stream: () => ({
    on() { return this; },
    async finalResponse() { throw new Error('native final failed'); },
    async *[Symbol.asyncIterator]() { yield { type: 'response.created' }; },
  }) } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const seen: string[] = [];
  await assert.rejects(async () => {
    for await (const event of client.responses.stream({ model: 'model', input: 'hello' })) seen.push(event.type);
  }, /native final failed/);
  assert.deepEqual(seen, ['response.created']);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('leaving a filtered Responses event iterator early marks the turn incomplete', async () => {
  let aborts = 0, submits = 0;
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) }),
    stream: () => ({
      on() { return this; },
      abort() { aborts++; },
      finalResponse: () => new Promise(() => {}),
      async *events(_name: string) { yield [{ type: 'response.created' }]; },
    }),
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  for await (const _event of stream.events('response.created')) break;
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.deepEqual([aborts, submits], [1, 0]);
  await client.close();
});

test('aborting a deferred Responses stream during recall never opens the native model stream', async () => {
  let releaseRecall!: () => void;
  const recallGate = new Promise<void>(resolve => { releaseRecall = resolve; });
  let markRecallStarted!: () => void;
  const recallStarted = new Promise<void>(resolve => { markRecallStarted = resolve; });
  let modelCalls = 0;
  const native = { responses: { stream: () => { modelCalls++; throw Error('native stream must not start'); } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/recipes/active')) { markRecallStarted(); await recallGate; return new Response(JSON.stringify({ status: 'not_found' })); }
    if (path.endsWith('/memory/recall')) return new Response(JSON.stringify({ items: [] }));
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await recallStarted;
  stream.abort();
  releaseRecall();
  await assert.rejects(stream.done(), /abort/i);
  assert.equal(modelCalls, 0);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('aborting a deferred Responses stream after native start marks it incomplete without submit', async () => {
  let markNativeStarted!: () => void;
  const nativeStarted = new Promise<void>(resolve => { markNativeStarted = resolve; });
  let nativeAborts = 0, submits = 0;
  const controller = new AbortController();
  const nativeStream = {
    controller,
    abort() { nativeAborts++; controller.abort(); },
    finalResponse: () => new Promise((_resolve, reject) => controller.signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })),
    async done() { if (controller.signal.aborted) throw new Error('aborted'); },
    async *[Symbol.asyncIterator]() { yield { type: 'response.created' }; },
  };
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }),
    stream: () => { markNativeStarted(); return nativeStream; },
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await nativeStarted;
  stream.abort();
  await assert.rejects(stream.done(), /aborted/);
  assert.equal(nativeAborts, 1);
  assert.equal(submits, 0);
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  await client.close();
});

test('the caller AbortSignal cancels an unconsumed deferred Responses stream', async () => {
  const controller = new AbortController();
  let nativeStarted!: () => void;
  const started = new Promise<void>(resolve => { nativeStarted = resolve; });
  let nativeAborts = 0, submits = 0;
  const native = { responses: {
    create: async (params: any) => params.text?.format
      ? { output_text: JSON.stringify({ activate: true, objective: 'task', task_family: 'general', reason: 'useful' }) }
      : undefined,
    stream: () => {
      nativeStarted();
      return { abort() { nativeAborts++; }, finalResponse: () => new Promise(() => {}), async done() {}, async *[Symbol.asyncIterator]() {} };
    },
  } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' }, { signal: controller.signal });
  await started;
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'native_running');
  controller.abort();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.deepEqual([nativeAborts, submits], [1, 0]);
  await client.close();
  void stream;
});

test('a Responses event stream ending with a function call awaits tool continuation', async () => {
  const response = { id: 'resp-call', status: 'completed', output: [{ type: 'function_call', call_id: 'c1', name: 'lookup', arguments: '{}' }] };
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }),
    stream: () => ({
      async *[Symbol.asyncIterator]() { yield { type: 'response.completed', response }; },
      async finalResponse() { return response; },
      async done() {},
      abort() {},
    }),
  } };
  let submits = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await stream.done();
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'awaiting_continuation');
  assert.equal(submits, 0);
  await client.close();
});

test('an incomplete Responses result without a tool call is not treated as tool continuation', async () => {
  let submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    return { id: 'resp-incomplete', status: 'incomplete', output: [{ type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'partial' }] }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((event: any) => event.id) }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  await client.responses.create({ model: 'model', input: 'hello' });
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.equal(submits, 0);
  await client.close();
});

test('an incomplete Responses event stream never submits its Run', async () => {
  const response = { id: 'resp-incomplete', status: 'incomplete', output: [{ type: 'message', role: 'assistant' }] };
  const native = { responses: {
    create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }),
    stream: () => ({
      async *[Symbol.asyncIterator]() { yield { type: 'response.incomplete', response }; },
      async finalResponse() { return response; },
      async done() {},
      abort() {},
    }),
  } };
  let submits = 0;
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  await stream.done();
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.equal(submits, 0);
  await client.close();
});

test('two transparent proxies serialize one Session through shared SQLite state', async () => {
  const order: string[] = [];
  const session = { sessionId: 'shared', getItems: async () => [] };
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>(resolve => { releaseFirst = resolve; });
  const native = { responses: { create: async (params: any) => {
    order.push(`model:${params.input}`);
    if (params.input === 'first') await firstGate;
    return { id: `resp-${params.input}`, status: 'completed', output_text: String(params.input), output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    order.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const path = statePath();
  const options = { apiKey: 'key', mode: 'disable' as const, session, fetch: fetcher, statePath: path };
  const first = cosens(native, options);
  const second = cosens({ responses: { create: native.responses.create } }, options);
  const firstRun = first.responses.create({ model: 'model', input: 'first' });
  void firstRun.then(() => {});
  while (!order.includes('model:first')) await new Promise(resolve => setTimeout(resolve, 1));
  const secondRun = second.responses.create({ model: 'model', input: 'second' });
  void secondRun.then(() => {});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.deepEqual(order, ['/api/v1/recipes/active', '/api/v1/memory/recall', 'model:first']);
  releaseFirst();
  await Promise.all([firstRun, secondRun]);
  assert.deepEqual(order, ['/api/v1/recipes/active', '/api/v1/memory/recall', 'model:first', '/api/v1/recipes/active', '/api/v1/memory/recall', 'model:second']);
  await first.close(); await second.close();
});

test('aborting a raw Responses stream through its native controller marks the turn incomplete', async () => {
  const controller = new AbortController();
  let submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    return { controller, async *[Symbol.asyncIterator]() { yield { type: 'response.created' }; } };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = await client.responses.create({ model: 'model', input: 'hello', stream: true });
  assert.equal(stream.controller, controller);
  stream.controller.abort();
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.equal(submits, 0);
  await client.close();
});

test('transparent reconcile after a lost submit receipt never repeats the model request', async () => {
  const path = statePath();
  let modelCalls = 0, prepares = 0, submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return { id: 'resp-1', status: 'completed', output_text: 'real result', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (url.pathname.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify(submits
      ? { run_id: 'r1', status: 'completed', result: { outcome: 'completed', summary: 'real result' } }
      : { run_id: 'r1', status: 'ready', snapshot: {} }));
    if (url.pathname.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (url.pathname.endsWith('/submit')) { submits++; return submits === 1
      ? new Response(JSON.stringify({ error: { message: 'temporary' } }), { status: 503 })
      : new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(url.pathname);
  };
  const options = { apiKey: 'key', mode: 'auto' as const, fetch: fetcher, statePath: path };
  const first = cosens(native, options);
  await first.responses.create({ model: 'model', input: 'hello' });
  const turnId = first.cosens.lastTurnId!;
  assert.equal(first.cosens.getTurn(turnId)?.state, 'completion_pending');
  await first.close();
  const restarted = cosens(native, options);
  const recovered = await restarted.cosens.reconcile(turnId);
  assert.equal(recovered.state, 'completed');
  assert.deepEqual([modelCalls, prepares, submits], [1, 1, 1]);
  await restarted.close();
});

test('reconcile repairs a partial observable ACK before tool continuation', async () => {
  let modelCalls = 0, eventPosts = 0, submits = 0;
  let persistedAtPost: any, aliasAtPost: any;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return modelCalls === 1
      ? { id: 'resp-call', status: 'completed', output: [{ type: 'function_call', call_id: 'c1', name: 'lookup', arguments: '{}' }] }
      : { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      eventPosts++;
      if (eventPosts === 1) {
        persistedAtPost = client.cosens.getTurn(client.cosens.lastTurnId);
        aliasAtPost = client.cosens.getTurn('response:resp-call');
      }
      return new Response(JSON.stringify({ durable_ack: eventPosts === 1 ? [] : JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  await client.responses.create({ model: 'model', input: 'hello' });
  const turnId = client.cosens.lastTurnId!;
  assert.equal(persistedAtPost?.state, 'observability_pending');
  assert.ok(persistedAtPost?.observablePending?.length);
  assert.equal(aliasAtPost?.turnId, turnId);
  assert.equal(client.cosens.getTurn(turnId)?.state, 'observability_pending');
  const repaired = await client.cosens.reconcile(turnId);
  assert.equal(repaired.state, 'awaiting_continuation');
  assert.deepEqual([modelCalls, eventPosts, submits], [1, 2, 0]);
  await client.responses.create({ model: 'model', previous_response_id: 'resp-call', input: [{ type: 'function_call_output', call_id: 'c1', output: 'found' }] });
  assert.equal(client.cosens.getTurn(turnId)?.state, 'completed');
  assert.equal(modelCalls, 2);
  assert.equal(submits, 1);
  await client.close();
});

test('tool response remains recoverable when Session reading fails before observable events', async () => {
  let modelCalls = 0, sessionReads = 0, eventPosts = 0, submits = 0, gatewayCalls = 0;
  const session = { sessionId: 'session-tool-recovery', async getItems() {
    sessionReads++;
    if (sessionReads === 2) throw new Error('Session temporarily unavailable');
    return [];
  } };
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return modelCalls === 1
      ? { id: 'resp-call', status: 'completed', output: [{ type: 'function_call', call_id: 'c1', name: 'lookup', arguments: '{}' }] }
      : { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    gatewayCalls++;
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) { eventPosts++; return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((event: any) => event.id) })); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const options = { apiKey: 'key', mode: 'auto' as const, session, fetch: fetcher, statePath: statePath() };
  const first = cosens(native, options);
  await first.responses.create({ model: 'model', input: 'hello' });
  const turnId = first.cosens.lastTurnId!;
  assert.equal(first.cosens.getTurn(turnId)?.state, 'observability_pending');
  assert.equal(first.cosens.getTurn('response:resp-call')?.turnId, turnId);
  assert.deepEqual([modelCalls, eventPosts, submits], [1, 0, 0]);
  await first.close();
  const continuation = { model: 'model', previous_response_id: 'resp-call', input: [{ type: 'function_call_output', call_id: 'c1', output: 'found' }] };
  const beforeWrongSession = gatewayCalls;
  const unreconciled = cosens(native, options);
  await assert.rejects(unreconciled.responses.create(continuation), /reconciliation/i);
  assert.deepEqual([modelCalls, gatewayCalls], [1, beforeWrongSession]);
  await unreconciled.close();
  for (const wrongSession of [
    { sessionId: 'another-session', async getItems() { return []; } },
    { sessionId: 'session-tool-recovery', sessionIncarnation: 'new-incarnation', async getItems() { return []; } },
  ]) {
    const wrong = cosens(native, { ...options, session: wrongSession });
    await assert.rejects(wrong.responses.create(continuation), /Session/i);
    assert.deepEqual([modelCalls, gatewayCalls], [1, beforeWrongSession]);
    await wrong.close();
  }
  const restarted = cosens(native, options);
  assert.equal((await restarted.cosens.reconcile(turnId)).state, 'awaiting_continuation');
  assert.equal(eventPosts, 1);
  await restarted.responses.create(continuation);
  assert.deepEqual([modelCalls, submits], [2, 1]);
  await restarted.close();
});

test('reconcile restores unsent final visible events before submit', async () => {
  let sessionReads = 0, modelCalls = 0, submits = 0, gatewayCalls = 0;
  const sent: any[] = [];
  const session = { sessionId: 'session-final-recovery', async getItems() {
    sessionReads++;
    if (sessionReads === 2) throw new Error('Session temporarily unavailable');
    return [];
  } };
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    gatewayCalls++;
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) { const events = JSON.parse(String(init?.body)).events; sent.push(...events); return new Response(JSON.stringify({ durable_ack: events.map((event: any) => event.id) })); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const options = { apiKey: 'key', mode: 'auto' as const, session, fetch: fetcher, statePath: statePath() };
  const first = cosens(native, options);
  await first.responses.create({ model: 'model', input: 'hello' });
  const turnId = first.cosens.lastTurnId!;
  assert.equal(first.cosens.getTurn(turnId)?.state, 'completion_pending');
  assert.deepEqual([modelCalls, sent.length, submits], [1, 0, 0]);
  await first.close();
  const beforeWrongSession = gatewayCalls;
  for (const wrongSession of [
    { sessionId: 'another-session', async getItems() { return []; } },
    { sessionId: 'session-final-recovery', sessionIncarnation: 'new-incarnation', async getItems() { return []; } },
  ]) {
    const wrong = cosens(native, { ...options, session: wrongSession });
    await assert.rejects(wrong.cosens.reconcile(turnId), /Session/i);
    assert.equal(gatewayCalls, beforeWrongSession);
    await wrong.close();
  }
  const restarted = cosens(native, options);
  assert.equal((await restarted.cosens.reconcile(turnId)).state, 'completed');
  assert.deepEqual(sent.map(event => event.id), [`visible:${turnId}:input`, `visible:${turnId}:output`]);
  assert.deepEqual([modelCalls, submits], [1, 1]);
  await restarted.close();
});

test('observable ACK cannot confirm an event that was not sent in its batch', async () => {
  let malformed = true, modelCalls = 0, submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    modelCalls++;
    return { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const ids = JSON.parse(String(init?.body)).events.map((event: any) => event.id);
      return new Response(JSON.stringify({ durable_ack: malformed ? [...ids, 'future-unsent-event'] : ids }));
    }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  await client.responses.create({ model: 'model', input: 'hello' });
  const turnId = client.cosens.lastTurnId!;
  const pending = client.cosens.getTurn(turnId);
  assert.equal(pending?.state, 'completion_pending');
  assert.equal(pending?.observablePending?.length, 2);
  assert.deepEqual(pending?.observableAckedIds ?? [], []);
  assert.deepEqual([modelCalls, submits], [1, 0]);
  malformed = false;
  assert.equal((await client.cosens.reconcile(turnId)).state, 'completed');
  assert.deepEqual([modelCalls, submits], [1, 1]);
  await client.close();
});

test('visible request and reply are not duplicated when the Session contains them', async () => {
  const items: any[] = [{ id: 'user-1', role: 'user', content: 'hello' }];
  const phase: string[] = [];
  const session = { sessionId: 'session-1', getItems: async () => { phase.push('session-read'); return items; } };
  const sentIds: string[] = [];
  const native = { responses: { create: async () => {
    items.push({ id: 'assistant-1', role: 'assistant', content: 'done' });
    return { id: 'response-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { phase.push('prepare'); items.push({ id: 'late', role: 'user', content: 'during prepare' }); return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const ids = JSON.parse(String(init?.body)).events.map((event: any) => event.id);
      sentIds.push(...ids);
      return new Response(JSON.stringify({ durable_ack: ids }));
    }
    if (path.endsWith('/submit')) return new Response(JSON.stringify({ status: 'completed' }));
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', decisionModel: 'model', session, fetch: fetcher, statePath: statePath() });
  // The decision call is distinct from the business call.
  native.responses.create = async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    items.push({ id: 'assistant-1', role: 'assistant', content: 'done' });
    return { id: 'response-1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  };
  await client.responses.create({ model: 'model', input: 'hello' });
  assert.ok(phase.indexOf('session-read') < phase.indexOf('prepare'));
  assert.deepEqual(sentIds, ['chat:user-1', 'chat:late', 'chat:assistant-1']);
  await client.close();
});

test('function call continuation uses one Run and reports call and tool result before submit', async () => {
  const order: string[] = [];
  const sent: any[] = [];
  let businessCalls = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    businessCalls++;
    order.push(`model:${businessCalls}`);
    if (businessCalls === 1) return { id: 'resp-call', status: 'completed', output: [{ type: 'function_call', call_id: 'call-1', name: 'lookup', arguments: '{}' }] };
    return { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { order.push('prepare'); return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const events = JSON.parse(String(init?.body)).events;
      sent.push(...events); order.push(`events:${events.map((event: any) => event.id).join(',')}`);
      return new Response(JSON.stringify({ durable_ack: events.map((event: any) => event.id) }));
    }
    if (path.endsWith('/submit')) { order.push('submit'); return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const first = await client.responses.create({ model: 'model', input: 'hello' });
  assert.equal(first.id, 'resp-call');
  assert.equal(order.includes('submit'), false);
  await client.responses.create({ model: 'model', previous_response_id: 'resp-call', input: [{ type: 'function_call_output', call_id: 'call-1', output: 'found' }] });
  assert.equal(order.filter(value => value === 'prepare').length, 1);
  assert.equal(order.at(-1), 'submit');
  assert.ok(sent.some(event => event.id.endsWith(':call:call-1') && event.kind === 'agent_action'));
  assert.ok(sent.some(event => event.id.endsWith(':tool:call-1') && event.kind === 'tool_result'));
  assert.ok(order.findIndex(value => value.includes(':tool:call-1')) < order.indexOf('model:2'));
  await client.close();
});

test('Chat tool-call IDs continue one Run and report the call and tool result', async () => {
  let decisions = 0, prepares = 0, business = 0, submits = 0;
  const sent: any[] = [];
  const native = {
    responses: { create: async () => { decisions++; return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }; } },
    chat: { completions: { create: async (params: any) => {
      business++;
      assert.ok(params.messages.some((item: any) => item.role === 'developer' && String(item.content).includes('CoSens frozen context')));
      return business === 1
        ? { id: 'chat-call', choices: [{ index: 0, finish_reason: 'tool_calls', message: { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'lookup', arguments: '{}' } }] } }] }
        : { id: 'chat-final', choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'answer' } }] };
    } } },
  };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: `r${prepares}`, status: 'ready', recipe: {}, context: {} })); }
    if (/\/runs\/r\d+$/.test(path) && init?.method === 'GET') return new Response(JSON.stringify({ run_id: path.split('/').at(-1), status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) {
      const events = JSON.parse(String(init?.body)).events;
      sent.push(...events);
      return new Response(JSON.stringify({ durable_ack: events.map((event: any) => event.id) }));
    }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const first = await client.chat.completions.create({ model: 'model', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(first.choices[0].finish_reason, 'tool_calls');
  await client.chat.completions.create({ model: 'model', messages: [
    { role: 'user', content: 'hello' },
    first.choices[0].message,
    { role: 'tool', tool_call_id: 'call-1', content: 'found' },
  ] });
  assert.deepEqual([decisions, prepares, business, submits], [1, 1, 2, 1]);
  assert.ok(sent.some(event => event.id.endsWith(':call:call-1') && event.kind === 'agent_action'));
  assert.ok(sent.some(event => event.id.endsWith(':tool:call-1') && event.kind === 'tool_result'));
  await client.chat.completions.create({ model: 'model', messages: [
    { role: 'user', content: 'hello' },
    first.choices[0].message,
    { role: 'tool', tool_call_id: 'call-1', content: 'found' },
    { role: 'user', content: 'next question' },
  ] });
  assert.deepEqual([decisions, prepares, business, submits], [2, 2, 3, 2]);
  assert.equal(sent.filter(event => event.id.endsWith(':tool:call-1')).length, 1);
  await client.close();
});

test('Chat tool-call IDs continue one disabled turn without a Run', async () => {
  let business = 0;
  const native = { chat: { completions: { create: async () => {
    business++;
    return business === 1
      ? { id: 'chat-call', choices: [{ finish_reason: 'tool_calls', message: { role: 'assistant', content: null, tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'lookup', arguments: '{}' } }] } }] }
      : { id: 'chat-final', choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: 'answer' } }] };
  } } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/recipes/active')) return new Response(JSON.stringify({ status: 'not_found' }));
    if (path.endsWith('/memory/recall')) return new Response(JSON.stringify({ items: [] }));
    throw Error(path);
  };
  const client = cosens(native, { mode: 'disable', apiKey: 'key', fetch: fetcher, statePath: statePath() });
  await client.chat.completions.create({ model: 'model', messages: [{ role: 'user', content: 'hello' }] });
  const turnId = client.cosens.lastTurnId;
  assert.equal(client.cosens.getTurn(turnId!)?.state, 'awaiting_continuation');
  await client.chat.completions.create({ model: 'model', messages: [{ role: 'tool', tool_call_id: 'call-1', content: 'found' }] });
  assert.equal(client.cosens.lastTurnId, turnId);
  assert.equal(client.cosens.getTurn(turnId!)?.state, 'completed');
  assert.equal(business, 2);
  await client.close();
});

test('Responses conversation reference links a tool output to the existing Run', async () => {
  let prepares = 0, decisions = 0, business = 0, submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) { decisions++; return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }; }
    business++;
    return business === 1
      ? { id: 'resp-call', status: 'completed', output: [{ type: 'function_call', call_id: 'c1', name: 'lookup', arguments: '{}' }] }
      : { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const first = await client.responses.create({ model: 'model', conversation: { id: 'conversation-1' }, input: 'hello' });
  assert.equal(first.id, 'resp-call');
  await client.responses.create({ model: 'model', conversation: { id: 'conversation-1' }, input: [{ type: 'function_call_output', call_id: 'c1', output: 'found' }] });
  assert.deepEqual([decisions, prepares, business, submits], [1, 1, 2, 1]);
  await client.close();
});

test('a conversation ID supplied by the Responses result links the next tool output to the same Run', async () => {
  let prepares = 0, decisions = 0, business = 0, submits = 0;
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) { decisions++; return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }; }
    business++;
    return business === 1
      ? { id: 'resp-call', conversation: { id: 'server-conversation' }, status: 'completed', output: [{ type: 'function_call', call_id: 'c1', name: 'lookup', arguments: '{}' }] }
      : { id: 'resp-final', status: 'completed', output_text: 'answer', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((e: any) => e.id) }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  await client.responses.create({ model: 'model', input: 'hello' });
  await client.responses.create({ model: 'model', conversation: 'server-conversation', input: [{ type: 'function_call_output', call_id: 'c1', output: 'found' }] });
  assert.deepEqual([decisions, prepares, business, submits], [1, 1, 2, 1]);
  await client.close();
});

test('Responses create stream retains its native controller and only submits after completion', async () => {
  const controller = new AbortController();
  const order: string[] = [];
  const nativeStream = {
    controller,
    async *[Symbol.asyncIterator]() {
      yield { type: 'response.created', response: { id: 'r1', status: 'in_progress', output: [] } };
      order.push('terminal');
      yield { type: 'response.completed', response: { id: 'r1', status: 'completed', output_text: 'done', output: [{ type: 'message' }] } };
    },
  };
  const native = { responses: { create: async (params: any) => {
    if (params.text?.format) return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) };
    return nativeStream;
  } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((event: any) => event.id) }));
    if (path.endsWith('/submit')) { order.push('submit'); return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = await client.responses.create({ model: 'model', input: 'hello', stream: true });
  assert.equal(stream.controller, controller);
  assert.equal(order.includes('submit'), false);
  const events = [];
  for await (const event of stream) events.push(event.type);
  assert.deepEqual(events, ['response.created', 'response.completed']);
  assert.deepEqual(order, ['terminal', 'submit']);
  await client.close();
});

test('Chat Completions raw stream submits its assembled final text after the last chunk', async () => {
  const submitted: any[] = [];
  const native = { chat: { completions: { create: async () => ({
    controller: new AbortController(),
    async *[Symbol.asyncIterator]() {
      yield { id: 'chat-1', choices: [{ index: 0, delta: { content: 'hel' }, finish_reason: null }] };
      yield { id: 'chat-1', choices: [{ index: 0, delta: { content: 'lo' }, finish_reason: 'stop' }] };
    },
  }) } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', recipe: {}, context: {} }));
    if (path.endsWith('/runs/run-1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'run-1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) return new Response(JSON.stringify({ durable_ack: JSON.parse(String(init?.body)).events.map((event: any) => event.id) }));
    if (path.endsWith('/submit')) { submitted.push(JSON.parse(String(init?.body))); return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', decisionModel: 'model', fetch: fetcher, statePath: statePath() });
  (native as any).responses = { create: async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }) };
  const stream = await client.chat.completions.create({ model: 'model', messages: [{ role: 'user', content: 'hello' }], stream: true });
  assert.equal(submitted.length, 0);
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  assert.equal(chunks.length, 2);
  assert.equal(submitted.length, 1);
  assert.equal(submitted[0].data.summary, 'hello');
  await client.close();
});

test('Chat Completions raw stream with tool calls waits for continuation without submit', async () => {
  let submits = 0;
  const native = { chat: { completions: { create: async () => ({
    controller: new AbortController(),
    async *[Symbol.asyncIterator]() {
      yield { id: 'chat-call', choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [{ id: 'call-1', type: 'function', function: { name: 'lookup', arguments: '{}' } }] }, finish_reason: null }] };
      yield { id: 'chat-call', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] };
    },
  }) } } };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/recipes/active')) return new Response(JSON.stringify({ status: 'not_found' }));
    if (path.endsWith('/memory/recall')) return new Response(JSON.stringify({ items: [] }));
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(`${path} ${init?.method}`);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  const stream = await client.chat.completions.create({ model: 'model', messages: [{ role: 'user', content: 'hello' }], stream: true });
  for await (const _chunk of stream) { /* consume */ }
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'awaiting_continuation');
  assert.equal(submits, 0);
  await client.close();
});

test('Chat stream tool-call fragments continue one Run and report the assembled call', async () => {
  let decisions = 0, prepares = 0, business = 0, submits = 0;
  const sent: any[] = [];
  const native = {
    responses: { create: async () => { decisions++; return { output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) }; } },
    chat: { completions: { create: async () => {
      business++;
      if (business > 1) return { id: 'chat-final', choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'done' } }] };
      return { controller: new AbortController(), async *[Symbol.asyncIterator]() {
        yield { id: 'chat-call', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call-1', type: 'function', function: { name: 'lookup', arguments: '{' } }] }, finish_reason: null }] };
        yield { id: 'chat-call', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '}' } }] }, finish_reason: 'tool_calls' }] };
      } };
    } } },
  };
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) { const events = JSON.parse(String(init?.body)).events; sent.push(...events); return new Response(JSON.stringify({ durable_ack: events.map((event: any) => event.id) })); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const client = cosens(native, { apiKey: 'key', mode: 'auto', fetch: fetcher, statePath: statePath() });
  const stream = await client.chat.completions.create({ model: 'model', messages: [{ role: 'user', content: 'hello' }], stream: true });
  for await (const _chunk of stream) { /* consume */ }
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'awaiting_continuation');
  await client.chat.completions.create({ model: 'model', messages: [{ role: 'tool', tool_call_id: 'call-1', content: 'found' }] });
  assert.deepEqual([decisions, prepares, business, submits], [1, 1, 2, 1]);
  assert.ok(sent.some(event => event.id.endsWith(':call:call-1') && event.payload.content.function.arguments === '{}'));
  assert.ok(sent.some(event => event.id.endsWith(':tool:call-1') && event.kind === 'tool_result'));
  await client.close();
});

test('closing the CoSens proxy does not close the caller-owned OpenAI client', async () => {
  let nativeClosed = false;
  const native = { responses: { create: async () => ({}) }, close: () => { nativeClosed = true; } };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', statePath: statePath() });
  await client.close();
  assert.equal(nativeClosed, false);
});

test('rewrapping an OpenAI client rejects a different CoSens credential', async () => {
  const native = { responses: { create: async () => ({}) } };
  const first = cosens(native, { apiKey: 'tenant-one', mode: 'disable', statePath: statePath(), decisionTimeoutMs: 2000, decisionMaxOutputTokens: 128 });
  assert.throws(() => cosens(first, { apiKey: 'tenant-two', mode: 'disable' }), /different/i);
  assert.throws(() => cosens(native, { decisionTimeoutMs: 3000 }), /different/i);
  assert.throws(() => cosens(native, { decisionMaxOutputTokens: 64 }), /different/i);
  await first.close();
});

test('repeating the same wrap shares a proxy and keeps its state open until both owners close', async () => {
  const native = { responses: { create: async () => ({}) } };
  const options = { apiKey: 'key', mode: 'disable' as const, statePath: statePath() };
  const first = cosens(native, options);
  const second = cosens(native, options);
  assert.equal(second, first);
  await first.close();
  assert.equal(second.cosens.getTurn('missing'), undefined);
  await second.close();
});

test('a completed explicit turn ID cannot be silently overwritten by another request', async () => {
  let modelCalls = 0;
  const native = { responses: { create: async () => {
    modelCalls++;
    return { id: `r${modelCalls}`, status: 'completed', output_text: 'done', output: [{ type: 'message' }] };
  } } };
  const fetcher: typeof fetch = async input => {
    const path = new URL(String(input)).pathname;
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', fetch: fetcher, statePath: statePath() });
  await client.cosens.scope({ turnId: 'same-turn' }, () => client.responses.create({ model: 'model', input: 'first' }));
  await assert.rejects(client.cosens.scope({ turnId: 'same-turn' }, () => client.responses.create({ model: 'model', input: 'second' })), /already completed|recovery/i);
  assert.equal(modelCalls, 1);
  await client.close();
});

test('Responses event stream reports a CoSens preflight failure before native streaming starts', async () => {
  let nativeCalls = 0;
  const native = { responses: { stream: () => { nativeCalls++; throw Error('must not stream'); } } };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', statePath: statePath(), fetch: async () => new Response(JSON.stringify({ error: { code: 'down', message: 'gateway unavailable' } }), { status: 503 }) });
  const stream = client.responses.stream({ model: 'model', input: 'hello' });
  const errors: Error[] = [];
  stream.on('error', (error: Error) => errors.push(error));
  await assert.rejects(stream.done(), /gateway unavailable/);
  assert.equal(errors.length, 1);
  assert.equal(nativeCalls, 0);
  await client.close();
});

test('unadapted image generation is forwarded and explicitly marked unsupported', async () => {
  let gatewayCalls = 0;
  const native = { images: { generate: async () => ({ data: [{ url: 'https://example.invalid/image' }] }) } };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', statePath: statePath(), fetch: async () => { gatewayCalls++; throw Error('gateway must not be used'); } });
  const result = await client.images.generate({ model: 'image-model', prompt: 'a tree' });
  assert.equal(result.data.length, 1);
  assert.equal(gatewayCalls, 0);
  assert.equal(client.cosens.rsiCapture, 'unsupported');
  await client.close();
});

test('read-only Responses retrieval is passed through without marking generation unsupported', async () => {
  let gatewayCalls = 0;
  const native = { responses: { retrieve: async (id: string) => ({ id, status: 'completed' }) } };
  const client = cosens(native, { apiKey: 'key', mode: 'disable', statePath: statePath(), fetch: async () => { gatewayCalls++; throw Error('gateway must not be used'); } });
  assert.equal((await client.responses.retrieve('resp-1')).id, 'resp-1');
  assert.equal(gatewayCalls, 0);
  assert.equal(client.cosens.rsiCapture, 'not_applicable');
  await client.close();
});
