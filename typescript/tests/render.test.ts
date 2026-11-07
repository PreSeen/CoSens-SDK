import assert from 'node:assert/strict';
import { test } from 'node:test';
import { inject } from '../src/context.ts';
import { renderExecutionRequest, renderGuidance } from '../src/render.ts';

const RECIPE = { title: 'Quarterly report', when: 'Writing a\n report', when_not: 'Casual chat',
  steps: [{ id: 's1', instruction: 'Collect figures', evidence_refs: [{ event_id: 'e1' }] },
    { id: 's2', instruction: 'Fill the template', evidence_refs: [{ event_id: 'e2' }] }],
  lessons: [{ when: 'x', do: 'Cite each figure', why: 'y', when_not: 'z', evidence_refs: [] }] };
const MEMORY = [{ id: 'm1', kind: 'source', text: 'Use the Q3 template.', refs: [{ source_id: 's' }], score: 0.4 }];

test('guidance lists steps, Memory and lessons without references', () => {
  const text = renderGuidance(RECIPE, MEMORY)!;
  assert.deepEqual(text.split('\n').slice(1), [
    'Recipe (provisional): Quarterly report', 'Use when: Writing a report', 'Do not use when: Casual chat',
    'Steps:', '1. Collect figures', '2. Fill the template',
    'Related Memory:', '- Use the Q3 template.', 'Lessons:', '- Cite each figure']);
  assert.ok(!text.includes('evidence') && !text.includes('score') && !text.includes('m1'));
});

test('guidance truncation is marked and bounded', () => {
  const recipe = { ...RECIPE, steps: Array.from({ length: 30 }, () => ({ instruction: 'step ' + 'x'.repeat(200) })) };
  const memory = Array.from({ length: 10 }, () => ({ text: 'memory ' + 'y'.repeat(2000) }));
  const text = renderGuidance(recipe, memory, 1000)!;
  assert.ok(text.length <= 1000);
  assert.ok(!text.includes('Lessons') && !text.includes('Related Memory'));
  assert.match(text.split('\n').at(-1)!, /^\[CoSens context truncated to 1000 characters;/);
});

test('inject skips empty context and uses only a matched bundle', () => {
  const params = { model: 'm', input: 'task' };
  assert.equal(inject(['responses', 'create'], params, { recipe: { status: 'not_found' }, snapshot: { evidence: [] }, context: { source: 'x' } }), params);
  const updated = inject(['chat', 'completions', 'create'], { messages: [] }, { recipe: { status: 'matched', bundle: RECIPE }, snapshot: { evidence: [] } });
  assert.equal(updated.messages[0].role, 'developer');
  assert.ok(updated.messages[0].content.includes('1. Collect figures'));
});

test('execution request rendering matches the Python SDK', () => {
  const request = { task_id: 'task_1', kind: 'revision', context_version: 3, trace_origin: 'revision',
    context: { objective: 'Write the Q3 report', artifacts: [{ artifact_id: 'a1', role: 'original_input', content_hash: 'sha256:abc' }] },
    feedback: [{ feedback: 'Use euros' }],
    method: { recipe_id: 'r1', recipe_candidate_id: 'c1', version: 2, recipe_sha256: 'f'.repeat(64), recipe: RECIPE },
    memory: { status: 'captured', scope: 'u', items: MEMORY } };
  assert.deepEqual(renderExecutionRequest(request).split('\n'), [
    '[CoSens frozen task execution request]', 'Task: task_1 (revision)', 'Objective: Write the Q3 report',
    'Input artifacts: a1 (original_input)', 'Feedback on the previous result:', '- Use euros',
    'Recipe (provisional): Quarterly report', 'Use when: Writing a report', 'Do not use when: Casual chat',
    'Steps:', '1. Collect figures', '2. Fill the template', 'Related Memory:', '- Use the Q3 template.',
    'Lessons:', '- Cite each figure']);
  assert.ok(renderExecutionRequest({ ...request, context: { objective: 'o '.repeat(5000) } }, 500).length <= 500);
});
