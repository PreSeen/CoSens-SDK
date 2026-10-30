import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import OpenAI from 'openai';
import { cosens } from '../src/index.ts';

const response = {
  id: 'resp-model-1', object: 'response', created_at: 1, model: 'test-model', status: 'completed',
  output: [{ id: 'msg-1', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: 'done', annotations: [] }] }],
};

test('the pinned OpenAI Responses stream keeps event and final-response methods through CoSens', async () => {
  const modelRequests: Array<{ url: string; body: any; headers: Headers }> = [];
  const modelFetch: typeof fetch = async (input, init) => {
    const url = String(input);
    const body = JSON.parse(String(init?.body));
    modelRequests.push({ url, body, headers: new Headers(init?.headers) });
    const events = [
      `event: response.created\ndata: ${JSON.stringify({ type: 'response.created', response: { ...response, status: 'in_progress', output: [] }, sequence_number: 0 })}\n\n`,
      `event: response.completed\ndata: ${JSON.stringify({ type: 'response.completed', response, sequence_number: 1 })}\n\n`,
    ].join('');
    return new Response(events, { headers: { 'content-type': 'text/event-stream' } });
  };
  const gatewayCalls: string[] = [];
  const gatewayFetch: typeof fetch = async input => {
    const path = new URL(String(input)).pathname; gatewayCalls.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  };
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: modelFetch });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: gatewayFetch, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const stream = client.responses.stream({ model: 'test-model', input: 'hello' });
  assert.equal(typeof stream.on, 'function');
  assert.equal(typeof stream.finalResponse, 'function');
  const seen: string[] = [];
  stream.on('response.completed', () => seen.push('completed'));
  const filteredEvents: string[] = [];
  const filteredDone = (async () => {
    for await (const [event] of stream.events('response.completed') as AsyncIterable<[any]>) filteredEvents.push(event.type);
  })();
  const wireEvents = [];
  for await (const event of stream) wireEvents.push(event.type);
  await filteredDone;
  assert.ok(wireEvents.includes('response.completed'));
  assert.deepEqual(seen, ['completed']);
  assert.deepEqual(filteredEvents, ['response.completed']);
  assert.equal((await stream.finalResponse()).id, 'resp-model-1');
  assert.deepEqual(gatewayCalls, ['/api/v1/recipes/active', '/api/v1/memory/recall']);
  assert.equal(modelRequests.length, 1);
  assert.equal(modelRequests[0]!.url, 'https://model.example/v1/responses');
  assert.equal(modelRequests[0]!.headers.get('authorization'), 'Bearer native-key');
  assert.equal(modelRequests[0]!.headers.get('x-auth-type'), null);
  await client.close();
});

test('the pinned OpenAI Chat stream keeps content events and finalChatCompletion', async () => {
  const modelFetch: typeof fetch = async () => {
    const chunks = [
      { id: 'chat-1', object: 'chat.completion.chunk', created: 1, model: 'test-model', choices: [{ index: 0, delta: { role: 'assistant', content: 'hel' }, finish_reason: null }] },
      { id: 'chat-1', object: 'chat.completion.chunk', created: 1, model: 'test-model', choices: [{ index: 0, delta: { content: 'lo' }, finish_reason: 'stop' }] },
    ];
    return new Response(chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
  };
  const gatewayFetch: typeof fetch = async input => new Response(JSON.stringify(new URL(String(input)).pathname.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: modelFetch });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: gatewayFetch, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const stream = client.chat.completions.stream({ model: 'test-model', messages: [{ role: 'user', content: 'hello' }] });
  const pieces: string[] = [];
  stream.on('content', (delta: string) => pieces.push(delta));
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  assert.equal(chunks.length, 2);
  assert.deepEqual(pieces, ['hel', 'lo']);
  const completion = await stream.finalChatCompletion();
  assert.equal(completion.choices[0]?.message.content, 'hello');
  await client.close();
});

test('the pinned OpenAI Chat stream keeps a tool call on the same CoSens Run', async () => {
  let modelCalls = 0, prepares = 0, submits = 0;
  const events: any[] = [];
  const modelFetch: typeof fetch = async (_input, init) => {
    modelCalls++;
    if (modelCalls === 1) {
      const chunks = [
        { id: 'chat-tool', object: 'chat.completion.chunk', created: 1, model: 'test-model', choices: [{ index: 0, delta: { role: 'assistant', tool_calls: [{ index: 0, id: 'call-1', type: 'function', function: { name: 'lookup', arguments: '{' } }] }, finish_reason: null }] },
        { id: 'chat-tool', object: 'chat.completion.chunk', created: 1, model: 'test-model', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: '}' } }] }, finish_reason: 'tool_calls' }] },
      ];
      return new Response(chunks.map(chunk => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n', { headers: { 'content-type': 'text/event-stream' } });
    }
    assert.equal(JSON.parse(String(init?.body)).messages.at(-2)?.tool_call_id, 'call-1');
    return new Response(JSON.stringify({ id: 'chat-final', object: 'chat.completion', created: 1, model: 'test-model', choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'done' } }] }), { headers: { 'content-type': 'application/json' } });
  };
  const gatewayFetch: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith('/prepare')) { prepares++; return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', recipe: {}, context: {} })); }
    if (path.endsWith('/runs/r1') && init?.method === 'GET') return new Response(JSON.stringify({ run_id: 'r1', status: 'ready', snapshot: {} }));
    if (path.endsWith('/session-events')) { const batch = JSON.parse(String(init?.body)).events; events.push(...batch); return new Response(JSON.stringify({ durable_ack: batch.map((event: any) => event.id) })); }
    if (path.endsWith('/submit')) { submits++; return new Response(JSON.stringify({ status: 'completed' })); }
    throw Error(path);
  };
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: modelFetch });
  (native.responses as any).create = async () => ({ output_text: JSON.stringify({ activate: true, objective: 'hello', task_family: 'general', reason: 'useful' }) });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'auto', fetch: gatewayFetch, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const stream = client.chat.completions.stream({ model: 'test-model', messages: [{ role: 'user', content: 'hello' }] });
  for await (const _chunk of stream) { /* consume native stream */ }
  const toolMessage = (await stream.finalChatCompletion()).choices[0]!.message;
  assert.equal(toolMessage.tool_calls?.[0]?.id, 'call-1');
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'awaiting_continuation');
  await client.chat.completions.create({ model: 'test-model', messages: [{ role: 'user', content: 'hello' }, toolMessage, { role: 'tool', tool_call_id: 'call-1', content: 'found' }] });
  assert.deepEqual([modelCalls, prepares, submits], [2, 1, 1]);
  assert.ok(events.some(event => event.id.endsWith(':call:call-1') && event.payload.content.function.arguments === '{}'));
  assert.ok(events.some(event => event.id.endsWith(':tool:call-1')));
  await client.close();
});

test('OpenAI withOptions keeps CoSens on the derived native client', async () => {
  const modelUrls: string[] = [];
  const gatewayCalls: string[] = [];
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async input => {
    modelUrls.push(String(input));
    return new Response(JSON.stringify(response), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async input => {
    const path = new URL(String(input)).pathname; gatewayCalls.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const derived = client.withOptions({ baseURL: 'https://other-model.example/v1' });
  assert.equal(typeof (derived as any).cosens.getTurn, 'function');
  await derived.responses.create({ model: 'test-model', input: 'hello' });
  assert.deepEqual(gatewayCalls, ['/api/v1/recipes/active', '/api/v1/memory/recall']);
  assert.deepEqual(modelUrls, ['https://other-model.example/v1/responses']);
  await (derived as any).close();
  await client.close();
});

test('native resource getters backed by private fields remain readable', async () => {
  class NativeResource { #value = 'native'; get metadata() { return this.#value; } }
  const native = { metadata: new NativeResource() };
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  assert.equal(client.metadata.metadata, 'native');
  await client.close();
});

test('the pinned OpenAI public resource methods preserve call arguments, return values and errors', async () => {
  const native = new OpenAI({ apiKey: 'native-key' });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const resources = ['decisions', 'completions', 'chat', 'embeddings', 'files', 'images', 'contentProvenanceChecks', 'audio', 'moderations', 'models', 'fineTuning', 'graders', 'vectorStores', 'safety', 'webhooks', 'beta', 'batches', 'uploads', 'admin', 'responses', 'live', 'realtime', 'conversations', 'evals', 'containers', 'skills', 'videos'] as const;
  const inventory = JSON.parse(readFileSync(new URL('./fixtures/openai-public-methods.json', import.meta.url), 'utf8')) as { version: string; roots: string[]; methods: string[] };
  const installed = JSON.parse(readFileSync(new URL('../node_modules/openai/package.json', import.meta.url), 'utf8')) as { version: string };
  assert.equal(installed.version, inventory.version);
  const isResource = (value: any) => {
    for (let proto = value && Object.getPrototypeOf(value); proto; proto = Object.getPrototypeOf(proto)) if (proto.constructor?.name === 'APIResource') return true;
    return false;
  };
  assert.deepEqual(Object.keys(native).filter(name => isResource((native as any)[name])).sort(), [...inventory.roots].sort());
  assert.deepEqual([...resources].sort(), [...inventory.roots].sort());
  const adapted = new Set(['responses.create', 'responses.parse', 'responses.stream', 'chat.completions.create', 'chat.completions.parse', 'chat.completions.stream']);
  let checked = 0;
  let runtimeChecked = 0;
  const methodPaths: string[] = [];
  const inspect = (original: any, wrapped: any, path: string, depth: number) => {
    assert.ok(depth <= 10, `OpenAI public resource tree is unexpectedly deep at ${path}`);
    const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(original)).filter(name => name !== 'constructor' && !name.startsWith('_'));
    for (const name of methods) {
      methodPaths.push(`${path}.${name}`);
      assert.equal(typeof wrapped[name], typeof original[name], `${original.constructor.name}.${name}`);
      checked++;
      if (typeof original[name] !== 'function' || adapted.has(`${path}.${name}`)) continue;
      const descriptor = Object.getOwnPropertyDescriptor(original, name);
      const marker = { path, name };
      const failure = new Error(`native ${path}.${name} error`);
      const first = { value: 1 }, second = Symbol('second');
      try {
        Object.defineProperty(original, name, { configurable: true, value: function (...args: unknown[]) {
          assert.equal(this, original);
          assert.deepEqual(args, [first, second]);
          return marker;
        } });
        assert.equal(wrapped[name](first, second), marker, `${path}.${name} return value`);
        Object.defineProperty(original, name, { configurable: true, value: function () { throw failure; } });
        assert.throws(() => wrapped[name](), error => error === failure, `${path}.${name} native error`);
        runtimeChecked++;
      } finally {
        if (descriptor) Object.defineProperty(original, name, descriptor);
        else delete original[name];
      }
    }
    for (const name of Object.keys(original).filter(name => !name.startsWith('_'))) {
      if (original[name] && typeof original[name] === 'object') inspect(original[name], wrapped[name], `${path}.${name}`, depth + 1);
    }
  };
  for (const name of resources) inspect((native as any)[name], (client as any)[name], name, 0);
  assert.deepEqual(methodPaths.sort(), inventory.methods);
  assert.equal(checked, inventory.methods.length);
  assert.equal(runtimeChecked, inventory.methods.length - adapted.size);
  await client.close();
});

test('pinned OpenAI models.list keeps its native pagination object and does not recall', async () => {
  let gatewayCalls = 0;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async () =>
    new Response(JSON.stringify({ object: 'list', data: [{ id: 'model-1', object: 'model', created: 1, owned_by: 'test' }] }), { headers: { 'content-type': 'application/json' } }) });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('CoSens should not be called'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const page = await client.models.list();
  assert.deepEqual(page.data.map(model => model.id), ['model-1']);
  assert.equal(typeof page.getNextPage, 'function');
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI JSON resources keep native requests across model and management roots', async () => {
  const requests: Array<{ path: string; method: string; headers: Headers }> = [];
  let gatewayCalls = 0;
  const modelFetch: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname;
    requests.push({ path, method: init?.method ?? 'GET', headers: new Headers(init?.headers) });
    const data = path.endsWith('/embeddings') ? { object: 'list', data: [{ object: 'embedding', index: 0, embedding: [0.1, 0.2] }], model: 'embedding-model', usage: { prompt_tokens: 1, total_tokens: 1 } }
      : path.endsWith('/moderations') ? { id: 'mod-1', model: 'moderation-model', results: [{ flagged: false, categories: {}, category_scores: {} }] }
      : path.endsWith('/images/generations') ? { created: 1, data: [{ url: 'https://image.example/generated.png' }] }
      : path.endsWith('/vector_stores') ? { object: 'list', data: [{ id: 'vs-1', object: 'vector_store' }], has_more: false }
      : path.endsWith('/organization/projects') ? { object: 'list', data: [{ id: 'proj-1', object: 'organization.project' }], has_more: false }
      : undefined;
    if (!data) throw Error(`Unexpected model path ${path}`);
    return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } });
  };
  const native = new OpenAI({ apiKey: 'native-key', adminAPIKey: 'native-admin-key', baseURL: 'https://model.example/v1', fetch: modelFetch });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const embedding = await client.embeddings.create({ model: 'embedding-model', input: 'hello' });
  const moderation = await client.moderations.create({ model: 'moderation-model', input: 'hello' });
  const image = await client.images.generate({ model: 'image-model', prompt: 'a circle' });
  const vectors = await client.vectorStores.list();
  const projects = await client.admin.organization.projects.list();
  assert.deepEqual(embedding.data[0]?.embedding, [0.1, 0.2]);
  assert.equal(moderation.results[0]?.flagged, false);
  assert.equal(image.data?.[0]?.url, 'https://image.example/generated.png');
  assert.equal(vectors.data[0]?.id, 'vs-1');
  assert.equal(projects.data[0]?.id, 'proj-1');
  assert.deepEqual(requests.map(request => [request.method, request.path]), [
    ['POST', '/v1/embeddings'], ['POST', '/v1/moderations'], ['POST', '/v1/images/generations'],
    ['GET', '/v1/vector_stores'], ['GET', '/v1/organization/projects'],
  ]);
  assert.ok(requests.every(request => request.headers.get('authorization') === (request.path.endsWith('/organization/projects') ? 'Bearer native-admin-key' : 'Bearer native-key') && request.headers.get('x-auth-type') === null));
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI read-only resources keep native HTTP behavior across additional roots', async () => {
  const requests: Array<{ path: string; headers: Headers }> = [];
  let gatewayCalls = 0;
  const native = new OpenAI({ apiKey: 'native-key', adminAPIKey: 'native-admin-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    const path = new URL(String(input)).pathname;
    requests.push({ path, headers: new Headers(init?.headers) });
    const data = path.endsWith('/conversations/conv-1') ? { id: 'conv-1', object: 'conversation' }
      : { object: 'list', data: [{ id: 'item-1' }], has_more: false };
    return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const calls: Array<() => Promise<any>> = [
    () => client.fineTuning.jobs.list(),
    () => client.webhooks.eventTypes.list(),
    () => client.beta.agents.list(),
    () => client.evals.list(),
    () => client.containers.list(),
    () => client.skills.list(),
    () => client.videos.list(),
    () => client.audio.voiceConsents.list(),
    () => client.conversations.retrieve('conv-1'),
  ];
  const results = [];
  for (const call of calls) results.push(await call());
  assert.ok(results.slice(0, -1).every(result => result.data[0]?.id === 'item-1'));
  assert.equal(results.at(-1)?.id, 'conv-1');
  assert.deepEqual(requests.map(request => request.path), [
    '/v1/fine_tuning/jobs', '/v1/webhook_event_types', '/v1/agents', '/v1/evals', '/v1/containers',
    '/v1/skills', '/v1/videos', '/v1/audio/voice_consents', '/v1/conversations/conv-1',
  ]);
  assert.ok(requests.every(request => request.headers.get('x-auth-type') === null && ['Bearer native-key', 'Bearer native-admin-key'].includes(request.headers.get('authorization') ?? '')));
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI files.content preserves the native binary Response', async () => {
  let gatewayCalls = 0;
  const bytes = new Uint8Array([0, 1, 2, 255]);
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async input => {
    assert.equal(String(input), 'https://model.example/v1/files/file-1/content');
    return new Response(bytes, { headers: { 'content-type': 'application/octet-stream', 'x-request-id': 'native-binary' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const response = await client.files.content('file-1');
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), bytes);
  assert.equal(response.headers.get('x-request-id'), 'native-binary');
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI file and transcription uploads preserve native multipart bodies', async () => {
  const requests: Array<{ path: string; headers: Headers; fileName: string; fileText: string; purposeOrModel: string }> = [];
  let gatewayCalls = 0;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    const path = new URL(String(input)).pathname;
    const headers = new Headers(init?.headers);
    const form = await new Response(init?.body as BodyInit, { headers }).formData();
    const file = form.get('file') as File;
    requests.push({ path, headers, fileName: file.name, fileText: await file.text(), purposeOrModel: String(form.get(path.endsWith('/files') ? 'purpose' : 'model')) });
    return new Response(JSON.stringify(path.endsWith('/files')
      ? { id: 'file-1', object: 'file', filename: 'source.txt', purpose: 'assistants', bytes: 6, created_at: 1 }
      : { text: 'transcribed' }), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const file = await client.files.create({ file: new File(['source'], 'source.txt', { type: 'text/plain' }), purpose: 'assistants' });
  const transcription = await client.audio.transcriptions.create({ file: new File(['speech'], 'speech.mp3', { type: 'audio/mpeg' }), model: 'whisper-1' });
  assert.equal(file.id, 'file-1');
  assert.equal(transcription.text, 'transcribed');
  assert.deepEqual(requests.map(request => [request.path, request.fileName, request.fileText, request.purposeOrModel]), [
    ['/v1/files', 'source.txt', 'source', 'assistants'],
    ['/v1/audio/transcriptions', 'speech.mp3', 'speech', 'whisper-1'],
  ]);
  assert.ok(requests.every(request => request.headers.get('authorization') === 'Bearer native-key' && request.headers.get('x-auth-type') === null && request.headers.get('content-type') === null));
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI nested upload parts preserve multipart data and request options', async () => {
  let gatewayCalls = 0;
  let observed: { path: string; dataName: string; bytes: number[]; auth: string | null; requestId: string | null } | undefined;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    const headers = new Headers(init?.headers);
    const form = await new Response(init?.body as BodyInit, { headers }).formData();
    const data = form.get('data') as File;
    observed = {
      path: new URL(String(input)).pathname,
      dataName: data.name,
      bytes: [...new Uint8Array(await data.arrayBuffer())],
      auth: headers.get('authorization'),
      requestId: headers.get('x-request-id'),
    };
    return new Response(JSON.stringify({ id: 'part-1', object: 'upload.part', created_at: 1, upload_id: 'upload-1' }), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const part = await client.uploads.parts.create('upload-1', { data: new File([new Uint8Array([0, 1, 255])], 'chunk.bin') }, { headers: { 'x-request-id': 'upload-test' } });
  assert.equal(part.id, 'part-1');
  assert.deepEqual(observed, { path: '/v1/uploads/upload-1/parts', dataName: 'chunk.bin', bytes: [0, 1, 255], auth: 'Bearer native-key', requestId: 'upload-test' });
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned Realtime call creation preserves native SDP transport', async () => {
  let gatewayCalls = 0;
  let observed: { url: string; headers: Headers; body: BodyInit | null | undefined } | undefined;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    observed = { url: String(input), headers: new Headers(init?.headers), body: init?.body };
    return new Response('answer-sdp', { headers: { 'content-type': 'application/sdp' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const result = await client.realtime.calls.create({ sdp: 'offer-sdp' });
  assert.equal(await result.text(), 'answer-sdp');
  assert.equal(observed?.url, 'https://model.example/v1/realtime/calls');
  assert.equal(observed?.headers.get('authorization'), 'Bearer native-key');
  assert.equal(observed?.headers.get('x-auth-type'), null);
  assert.equal(observed?.headers.get('content-type'), 'application/sdp');
  assert.equal(observed?.headers.get('accept'), 'application/sdp');
  assert.equal(observed?.body, 'offer-sdp');
  assert.equal(gatewayCalls, 0);
  assert.equal(client.cosens.rsiCapture, 'unsupported');
  await client.close();
});

test('pinned OpenAI batches.list keeps native cursor pagination', async () => {
  let gatewayCalls = 0;
  const modelUrls: string[] = [];
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async input => {
    const url = new URL(String(input));
    modelUrls.push(url.toString());
    const after = url.searchParams.get('after');
    return new Response(JSON.stringify({ object: 'list', data: [{ id: after ? 'batch-2' : 'batch-1', object: 'batch' }], has_more: !after }), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const first = await client.batches.list({ limit: 1 });
  assert.equal(first.data[0]?.id, 'batch-1');
  assert.equal(first.hasNextPage(), true);
  const second = await first.getNextPage();
  assert.equal(second.data[0]?.id, 'batch-2');
  assert.equal(second.hasNextPage(), false);
  assert.deepEqual(modelUrls, ['https://model.example/v1/batches?limit=1', 'https://model.example/v1/batches?limit=1&after=batch-1']);
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI APIPromise retains withResponse and asResponse behavior', async () => {
  let modelCalls = 0;
  const gatewayFetch: typeof fetch = async input => new Response(JSON.stringify(new URL(String(input)).pathname.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async () => {
    modelCalls++;
    return new Response(JSON.stringify(response), { headers: { 'content-type': 'application/json', 'x-request-id': `req-${modelCalls}` } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: gatewayFetch, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const withResponse = await client.responses.create({ model: 'test-model', input: 'hello' }).withResponse();
  assert.equal(withResponse.data.id, response.id);
  assert.equal(withResponse.response.headers.get('x-request-id'), 'req-1');
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'completed');
  const raw = await client.responses.create({ model: 'test-model', input: 'again' }).asResponse();
  assert.equal(raw.headers.get('x-request-id'), 'req-2');
  assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.state, 'incomplete');
  assert.equal(modelCalls, 2);
  await client.close();
});

test('pinned OpenAI read-only resource errors retain native error type and skip recall', async () => {
  let gatewayCalls = 0;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', maxRetries: 0, fetch: async () =>
    new Response(JSON.stringify({ error: { message: 'missing model', type: 'invalid_request_error' } }), { status: 404, headers: { 'content-type': 'application/json' } }) });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  await assert.rejects(client.models.retrieve('absent'), error => error instanceof OpenAI.NotFoundError && error.status === 404);
  assert.equal(gatewayCalls, 0);
  await client.close();
});

test('pinned OpenAI Responses parse and Chat create/parse use the same preflight contract', async () => {
  const modelPaths: string[] = [];
  const gatewayPaths: string[] = [];
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async input => {
    const path = new URL(String(input)).pathname;
    modelPaths.push(path);
    const body = path.endsWith('/responses') ? response : {
      id: 'chat-1', object: 'chat.completion', created: 1, model: 'test-model',
      choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'done' } }],
    };
    return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async input => {
    const path = new URL(String(input)).pathname;
    gatewayPaths.push(path);
    return new Response(JSON.stringify(path.endsWith('/recipes/active') ? { status: 'not_found' } : { items: [] }));
  }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const responsesParsed = await client.responses.parse({ model: 'test-model', input: 'hello' });
  const chatCreated = await client.chat.completions.create({ model: 'test-model', messages: [{ role: 'user', content: 'hello' }] });
  const chatParsed = await client.chat.completions.parse({ model: 'test-model', messages: [{ role: 'user', content: 'hello' }] });
  assert.equal(responsesParsed.id, response.id);
  assert.equal(chatCreated.choices[0]?.message.content, 'done');
  assert.equal(chatParsed.choices[0]?.message.content, 'done');
  assert.deepEqual(modelPaths, ['/v1/responses', '/v1/chat/completions', '/v1/chat/completions']);
  assert.deepEqual(gatewayPaths, [
    '/api/v1/recipes/active', '/api/v1/memory/recall',
    '/api/v1/recipes/active', '/api/v1/memory/recall',
    '/api/v1/recipes/active', '/api/v1/memory/recall',
  ]);
  await client.close();
});

test('pinned legacy Completions stays callable and reports unsupported RSI capture', async () => {
  let gatewayCalls = 0;
  const native = new OpenAI({ apiKey: 'native-key', baseURL: 'https://model.example/v1', fetch: async input => {
    assert.equal(String(input), 'https://model.example/v1/completions');
    return new Response(JSON.stringify({ id: 'completion-1', object: 'text_completion', created: 1, model: 'test-model', choices: [{ index: 0, text: 'done', finish_reason: 'stop' }] }), { headers: { 'content-type': 'application/json' } });
  } });
  const client = cosens(native, { apiKey: 'friday-key', mode: 'disable', fetch: async () => { gatewayCalls++; throw Error('unexpected recall'); }, statePath: join(mkdtempSync(join(tmpdir(), 'cosens-compat-')), 'state.sqlite3') });
  const result = await client.completions.create({ model: 'test-model', prompt: 'hello' });
  assert.equal(result.choices[0]?.text, 'done');
  assert.equal(client.cosens.rsiCapture, 'unsupported');
  assert.equal(gatewayCalls, 0);
  await client.close();
});
