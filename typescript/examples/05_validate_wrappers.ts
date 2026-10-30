/** Real CoSens gateway with an offline OpenAI model; --write creates synthetic Runs. */
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { randomUUID } from 'node:crypto';
import OpenAI from 'openai';
import { Agent } from '@openai/agents';
import { CoSensAgentRunner, cosens } from '../src/index.ts';
import { fridayKey } from './common.ts';

const apiKey = fridayKey();
const write = process.argv.includes('--write');
const outputIndex = process.argv.indexOf('--output');
const outputPath = outputIndex >= 0 ? process.argv[outputIndex + 1] : undefined;
if (outputIndex >= 0 && !outputPath) throw new Error('--output requires a file path');
const tag = `sdk-ts-gateway-${randomUUID()}`;
const directory = mkdtempSync(join(tmpdir(), 'cosens-ts-gateway-'));
const requests: Array<{ method: string; path: string; status: number; authType: string; authorization: string; identityHeaderSent: boolean; body?: unknown; requestId: string | null }> = [];
const gatewayFetch: typeof fetch = async (input, init) => {
  const headers = new Headers(init?.headers);
  assert.equal(headers.get('x-auth-type'), 'api_key');
  assert.equal(headers.get('authorization'), `Bearer ${apiKey}`);
  assert.equal(headers.has('x-user-id') || headers.has('x_user_id'), false);
  const url = new URL(String(input));
  assert.equal(url.protocol, 'https:');
  assert.ok(url.pathname.startsWith('/api/'));
  const response = await fetch(input, init);
  requests.push({
    method: init?.method ?? 'GET', path: url.pathname + url.search, status: response.status,
    authType: headers.get('x-auth-type')!, authorization: 'Bearer <redacted>', identityHeaderSent: false,
    ...(init?.body ? { body: JSON.parse(String(init.body)) } : {}), requestId: response.headers.get('x-request-id'),
  });
  return response;
};

async function transparent(scenario: 'disable' | 'auto_false' | 'auto_true') {
  let modelCalls = 0;
  const modelFetch: typeof fetch = async (_input, init) => {
    const headers = new Headers(init?.headers);
    assert.equal(headers.get('authorization'), 'Bearer offline-model-only');
    assert.equal(headers.has('x-auth-type'), false);
    const body = JSON.parse(String(init?.body));
    const decision = body.text?.format?.name === 'cosens_decision';
    if (!decision) assert.match(body.instructions, /CoSens frozen context/);
    modelCalls++;
    const text = decision
      ? JSON.stringify({ activate: scenario === 'auto_true', objective: `${tag}:${scenario}`, task_family: 'general', reason: 'synthetic gateway validation' })
      : 'local fake model result';
    const response = {
      id: `response-${randomUUID()}`, object: 'response', created_at: 1, model: 'offline-model', status: 'completed',
      output: [{ id: `message-${randomUUID()}`, type: 'message', role: 'assistant', status: 'completed', content: [{ type: 'output_text', text, annotations: [] }] }],
    };
    return new Response(JSON.stringify(response), { headers: { 'content-type': 'application/json' } });
  };
  const native = new OpenAI({ apiKey: 'offline-model-only', baseURL: 'https://offline-model.invalid/v1', fetch: modelFetch, maxRetries: 0 });
  const client = cosens(native, { apiKey, mode: scenario === 'disable' ? 'disable' : 'auto', fetch: gatewayFetch, statePath: join(directory, `${scenario}.sqlite3`) });
  try {
    const response = await client.responses.create({ model: 'offline-model', input: `${tag}:${scenario} synthetic request` });
    assert.equal(response.output_text, 'local fake model result');
    const turn = client.cosens.getTurn(client.cosens.lastTurnId);
    assert.equal(turn?.state, 'completed');
    assert.equal(Boolean(turn?.runId), scenario === 'auto_true');
    assert.equal(modelCalls, scenario === 'disable' ? 1 : 2);
    return { status: 'passed', runId: turn?.runId ?? null, modelCalls };
  } finally { await client.close(); }
}

async function explicit() {
  const agent = new Agent({ name: 'sdk-ts-gateway-check', instructions: 'Synthetic test only', model: 'offline-model' });
  let nativeCalls = 0;
  const runner = new CoSensAgentRunner(agent, {
    apiKey, mode: 'disable', fetch: gatewayFetch, statePath: join(directory, 'agent.sqlite3'),
    runner: { run: async (activeAgent, input) => {
      nativeCalls++;
      assert.match(activeAgent.instructions, /CoSens frozen context/);
      assert.match(input, /synthetic request/);
      return { finalOutput: 'local fake Agent result', interruptions: [] };
    } },
  });
  try {
    const result = await runner.run(agent, `${tag}:explicit synthetic request`);
    assert.equal(result.finalOutput, 'local fake Agent result');
    const turn = runner.cosens.getTurn(runner.cosens.lastTurnId);
    assert.equal(turn?.state, 'completed');
    assert.equal(nativeCalls, 1);
    return { status: 'passed', runId: turn?.runId ?? null, nativeCalls };
  } finally { await runner.close(); }
}

try {
  const results: Record<string, unknown> = {
    disable: await transparent('disable'),
    autoFalse: await transparent('auto_false'),
  };
  if (write) {
    results.autoTrue = await transparent('auto_true');
    results.explicit = await explicit();
  }
  const report = { tag, write, results, requests };
  if (outputPath) {
    mkdirSync(dirname(outputPath), { recursive: true });
    writeFileSync(outputPath, JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify({ tag, write, results, requestCount: requests.length, outputPath: outputPath ?? null }));
} finally {
  rmSync(directory, { recursive: true, force: true });
}
