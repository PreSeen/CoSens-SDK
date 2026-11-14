import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import OpenAI from 'openai';
import { cosens } from '../src/index.ts';
import { DEFAULT_DECISION_MAX_OUTPUT_TOKENS } from '../src/decision.ts';

const statePath = () => join(mkdtempSync(join(tmpdir(), 'cosens-latency-')), 'state.sqlite3');
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json' } });
const chat = (content: unknown) => json({ id: 'decision', object: 'chat.completion', created: 1, model: 'm',
  choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify(content) } }] });
const RESPONSE = { id: 'resp-1', object: 'response', created_at: 1, model: 'm', status: 'completed',
  output: [{ id: 'msg-1', type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text: 'done', annotations: [] }] }] };
const ACTIVATE = { activate: true, reason: 'task' };

function candidate(steps = 3) {
  return { recipe_candidate_id: 'candidate_1', recipe_id: 'recipe_1', status: 'candidate', current: true, version: 1,
    recipe: { title: 'Quarterly report', when: 'Writing a report', when_not: 'Chat',
      steps: Array.from({ length: steps }, (_, n) => ({ id: `s${n + 1}`, instruction: `Do step ${n + 1}`, evidence_refs: [{ event_id: `e${n + 1}` }] })),
      lessons: [{ when: 'Always', do: 'Cite the source', why: 'Trust', when_not: 'Never', evidence_refs: [{ event_id: 'e1' }] }] } };
}

/** Minimal service: optional Recipe candidates, Memory, task and execution. */
function algorithm(candidates: any[] = []) {
  const paths: string[] = [];
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname.replace(/^\/api/, '');
    paths.push(path);
    if (path === '/recipe-candidates') return json({ items: candidates.map(c => ({ recipe_candidate_id: c.recipe_candidate_id, status: 'candidate', current: true, title: c.recipe.title })), has_more: false });
    if (path.startsWith('/recipe-candidates/')) return json(candidates.find(c => path.endsWith('/' + c.recipe_candidate_id)));
    if (path === '/v1/memory/recall') return json({ items: [{ id: 'm1', kind: 'source', text: 'Reports use the Q3 template.', refs: [{ source_id: 's1' }], score: 0.9 }] });
    if (path === '/artifacts') return json({ artifact_id: 'artifact_1', content_hash: 'sha256:x' });
    if (path === '/tasks') return json({ task_id: 'task_1', status: 'ready' });
    if (path.endsWith('/executions')) {
      const body = JSON.parse(String(init?.body));
      const chosen = candidates.find(c => c.recipe_id === body.recipe_id);
      return json({ execution_id: 'execution_1', status: 'awaiting_execution', request_sha256: 'sha',
        request: { method: chosen ? { recipe_id: chosen.recipe_id, recipe_candidate_id: chosen.recipe_candidate_id, version: chosen.version } : null } });
    }
    if (path.endsWith('/result')) return json({ status: 'completed', revision_id: 'revision_1' });
    throw new Error(path);
  };
  return { paths, fetcher };
}

test('the decision is a single unretried call and falls back to the native request', async () => {
  const decisionBodies: any[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    if (String(input).endsWith('/chat/completions')) { decisionBodies.push(JSON.parse(String(init?.body))); return json({ error: { message: 'overloaded' } }, 500); }
    return json(RESPONSE);
  } });
  const service = algorithm();
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath() });
  try {
    const response = await client.responses.create({ model: 'm', input: 'Produce a report' });
    assert.equal(response.id, 'resp-1');
    assert.equal(decisionBodies.length, 1);
    assert.equal(decisionBodies[0].max_completion_tokens, 1024);
    assert.equal(client.cosens.rsiCapture, 'decision_failed');
    assert.equal(native.maxRetries, 2);
  } finally { await client.close(); }
});

test('decisionClient and decisionExtraBody cover the decision and Recipe match', async () => {
  const nativePaths: string[] = [];
  const decisionBodies: any[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async input => { nativePaths.push(new URL(String(input)).pathname); return json(RESPONSE); } });
  const decisionClient = new OpenAI({ apiKey: 'decision-key', baseURL: 'https://decider.example/v1', fetch: async (_input, init) => {
    const body = JSON.parse(String(init?.body));
    decisionBodies.push(body);
    return chat(body.response_format.json_schema.name === 'cosens_recipe_match' ? { candidate_id: 'candidate_1' } : ACTIVATE);
  } });
  const service = algorithm([candidate()]);
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath(),
    decisionClient, decisionExtraBody: { enable_thinking: false } });
  try {
    await client.responses.create({ model: 'm', input: 'Produce a report' });
    assert.deepEqual(nativePaths, ['/v1/responses']);
    assert.deepEqual(decisionBodies.map(body => body.response_format.json_schema.name), ['cosens_decision', 'cosens_recipe_match']);
    assert.ok(decisionBodies.every(body => body.enable_thinking === false && body.max_completion_tokens === 1024));
    const matchInput = decisionBodies[1].messages[0].content;
    assert.ok(matchInput.includes('Writing a report') && !matchInput.includes('Do step 1') && !matchInput.includes('evidence_refs'));
    assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.frozenContext.recipe.recipe_id, 'recipe_1');
  } finally { await client.close(); }
});

test('an empty Recipe list skips the match call and is cached', async () => {
  const decisionNames: string[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    if (String(input).endsWith('/chat/completions')) { decisionNames.push(JSON.parse(String(init?.body)).response_format.json_schema.name); return chat(ACTIVATE); }
    return json(RESPONSE);
  } });
  const service = algorithm();
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath() });
  try {
    await client.responses.create({ model: 'm', input: 'Produce a report' });
    await client.responses.create({ model: 'm', input: 'Produce another report' });
    assert.deepEqual(decisionNames, ['cosens_decision', 'cosens_decision']);
    assert.equal(service.paths.filter(path => path === '/recipe-candidates').length, 1);
    assert.equal(service.paths.filter(path => path === '/v1/memory/recall').length, 2);
  } finally { await client.close(); }
});

test('injected context is compact and bounded by maxContextChars', async () => {
  const sent: any[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    const body = JSON.parse(String(init?.body));
    if (String(input).endsWith('/chat/completions')) return chat(body.response_format.json_schema.name === 'cosens_recipe_match' ? { candidate_id: 'candidate_1' } : ACTIVATE);
    sent.push(body);
    return json(RESPONSE);
  } });
  const service = algorithm([candidate(30)]);
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath(), maxContextChars: 400 });
  try {
    await client.responses.create({ model: 'm', input: 'Produce a report', instructions: 'Be brief.' });
    const instructions: string = sent[0].instructions;
    assert.ok(instructions.startsWith('Be brief.\n\n[CoSens context:'));
    const block = instructions.slice('Be brief.\n\n'.length);
    assert.ok(block.length <= 400);
    assert.ok(block.includes('Recipe (provisional): Quarterly report') && block.includes('1. Do step 1'));
    assert.ok(!block.includes('evidence_refs') && !block.includes('score'));
    assert.match(block.split('\n').at(-1)!, /^\[CoSens context truncated to 400 characters;/);
  } finally { await client.close(); }
});

test('a declined turn makes no Recipe or Memory lookup and sends the request unchanged', async () => {
  const sent: any[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    const body = JSON.parse(String(init?.body));
    if (String(input).endsWith('/chat/completions')) return chat({ activate: false, reason: 'routine' });
    sent.push(body);
    return json(RESPONSE);
  } });
  const service = algorithm([candidate()]);
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath() });
  try {
    const request = { model: 'm', input: 'Say hi', instructions: 'Preserve this instruction' };
    await client.responses.create(request);
    assert.deepEqual(service.paths, []);
    assert.deepEqual(sent, [request]);
    assert.equal(client.cosens.rsiCapture, 'skipped');
    const turn = client.cosens.getTurn(client.cosens.lastTurnId);
    assert.equal(turn?.frozenContext, undefined);
    assert.equal(turn?.executionId, undefined);
  } finally { await client.close(); }
});

test('maxContextChars and decisionExtraBody are validated', () => {
  const native = new OpenAI({ apiKey: 'model-key' });
  assert.throws(() => cosens(native, { apiKey: 'k', maxContextChars: 10, statePath: statePath() }), /maxContextChars/);
  assert.throws(() => cosens(new OpenAI({ apiKey: 'model-key' }), { apiKey: 'k', decisionExtraBody: [] as any, statePath: statePath() }), /decisionExtraBody/);
});

// Largest completion (reasoning + JSON) of a successful policy call measured on the docs'
// default model deepseek-flash via llm.cosens.ai, 2026-11-06: 139 tokens for a decision,
// 294 with a linked feedback turn, 146 for a Recipe match (84 calls). Keep at least 3x headroom.
const MEASURED_POLICY_COMPLETION_TOKENS = 294;

test('the default decision budget fits a measured reasoning reply', async () => {
  assert.ok(DEFAULT_DECISION_MAX_OUTPUT_TOKENS >= 3 * MEASURED_POLICY_COMPLETION_TOKENS);
  const budgets: number[] = [];
  const bodies: any[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async (input, init) => {
    if (!String(input).endsWith('/chat/completions')) return json(RESPONSE);
    const body = JSON.parse(String(init?.body));
    budgets.push(body.max_completion_tokens); bodies.push(body);
    // Behaves like a reasoning model: a budget below the reply size is cut off.
    if (body.max_completion_tokens < MEASURED_POLICY_COMPLETION_TOKENS) return json({ id: 'd', object: 'chat.completion', created: 1, model: 'm', choices: [{ index: 0, finish_reason: 'length', message: { role: 'assistant', content: '' } }] });
    return chat({ activate: true, reason: 'multi-step report' });
  } });
  const service = algorithm();
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: service.fetcher, statePath: statePath() });
  try {
    await client.responses.create({ model: 'deepseek-flash', input: 'Produce a quarterly report' });
    assert.deepEqual(budgets, [DEFAULT_DECISION_MAX_OUTPUT_TOKENS]);
    assert.deepEqual(bodies[0].response_format.json_schema.schema.required, ['activate', 'reason']);
    const turn = client.cosens.getTurn(client.cosens.lastTurnId);
    assert.equal(turn?.decision.capture, 'active');
    assert.equal(turn?.objective, 'Produce a quarterly report');
    assert.equal(turn?.executionId, 'execution_1');
  } finally { await client.close(); }
});

test('a request without user text makes no decision call', async () => {
  const paths: string[] = [];
  const native = new OpenAI({ apiKey: 'model-key', baseURL: 'https://model.example/v1', fetch: async input => { paths.push(new URL(String(input)).pathname); return json(RESPONSE); } });
  const client = cosens(native, { apiKey: 'service-key', mode: 'auto', fetch: algorithm().fetcher, statePath: statePath() });
  try {
    await client.responses.create({ model: 'm', input: [{ role: 'user', content: [{ type: 'input_image', image_url: 'data:,' }] }] as any });
    assert.deepEqual(paths, ['/v1/responses']);
    assert.equal(client.cosens.getTurn(client.cosens.lastTurnId)?.decision.capture, 'no_user_text');
  } finally { await client.close(); }
});
