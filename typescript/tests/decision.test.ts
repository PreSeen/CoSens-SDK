import assert from 'node:assert/strict';
import { test } from 'node:test';
import { decide } from '../src/decision.ts';
import { RSIDecisionUnavailable } from '../src/errors.ts';

test('decision timeout stays in OpenAI request options, outside the model JSON body', async () => {
  let body: any;
  let options: any;
  const native = { responses: { create: async (request: any, requestOptions: any) => {
    body = request; options = requestOptions;
    return { output_text: JSON.stringify({ activate: false, objective: 'task', task_family: 'general', reason: 'no run' }) };
  } } };
  await decide(native, 'decision-model', 'task', ['search'], 5000, 256);
  assert.equal(body.timeout, undefined);
  assert.equal(options.timeout, 5000);
  assert.equal(body.max_output_tokens, 256);
  assert.equal(body.store, false);
});

test('decision parser rejects missing reason and extra model fields', async () => {
  for (const value of [
    { activate: true, objective: 'task', task_family: 'general' },
    { activate: true, objective: 'task', task_family: 'general', reason: 'yes', unauthorized: true },
  ]) {
    const native = { responses: { create: async () => ({ output_text: JSON.stringify(value) }) } };
    await assert.rejects(decide(native, 'decision-model', 'task', [], 5000, 256), RSIDecisionUnavailable);
  }
});
