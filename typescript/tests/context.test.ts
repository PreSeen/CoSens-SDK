import assert from 'node:assert/strict';
import { test } from 'node:test';
import { userText } from '../src/context.ts';

test('multimodal user text extraction ignores non-string text payloads', () => {
  const extracted = userText(['responses', 'create'], { input: [
    { role: 'user', content: [{ type: 'input_text', text: 'hello' }, { type: 'input_text', text: { value: 'not text' } }, { type: 'input_image', image_url: 'data:image/png;base64,...' }] },
  ] });
  assert.equal(extracted, 'hello');
});

test('classification uses only the latest visible user message from Chat history', () => {
  assert.equal(userText(['chat', 'completions', 'create'], { messages: [
    { role: 'user', content: 'old task' },
    { role: 'assistant', content: 'old answer' },
    { role: 'user', content: [{ type: 'text', text: 'current ' }, { type: 'text', text: 'task' }] },
  ] }), 'current \ntask');
});
