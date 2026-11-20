import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer, type IncomingHttpHeaders } from 'node:http';
import type { AddressInfo } from 'node:net';
import test from 'node:test';
import { z } from 'zod';
import { requestCompletion } from '../src/rsi/chat-completions.js';
import { LocalConfigSchema } from '../src/rsi/models.js';
import { parseStructured } from '../src/rsi/runtime.js';

async function withEndpoint(
  run: (
    baseUrl: string,
    seen: { headers: IncomingHttpHeaders; body: Record<string, unknown> }[],
  ) => Promise<void>,
) {
  const seen: { headers: IncomingHttpHeaders; body: Record<string, unknown> }[] =
    [];
  const server = createServer((request, response) => {
    let text = '';
    request.on('data', (chunk) => (text += chunk));
    request.on('end', () => {
      seen.push({ headers: request.headers, body: JSON.parse(text) });
      response.setHeader('content-type', 'application/json');
      response.end(
        JSON.stringify({
          choices: [
            {
              message: { role: 'assistant', content: 'ok' },
              finish_reason: 'stop',
            },
          ],
        }),
      );
    });
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}/v1`, seen);
  } finally {
    server.close();
  }
}

test('completion budget is left to the endpoint unless configured', async () => {
  await withEndpoint(async (base_url, seen) => {
    const model = (extra: object) =>
      LocalConfigSchema.parse({ model: { model: 'm', base_url, ...extra } })
        .model;
    await requestCompletion(model({}), {}, [], []);
    await requestCompletion(model({ max_tokens: 16384 }), {}, [], []);
    assert.equal('max_tokens' in seen[0].body, false);
    assert.equal(seen[1].body.max_tokens, 16384);
  });
});

test('model requests name the SDK in the User-Agent', async () => {
  await withEndpoint(async (base_url, seen) => {
    const model = LocalConfigSchema.parse({ model: { model: 'm', base_url } })
      .model;
    await requestCompletion(model, { 'x-api-key': 'test-token' }, [], []);
    assert.match(String(seen[0].headers['user-agent']), /^cosens-sdk-typescript\//);
    assert.equal(seen[0].headers['x-api-key'], 'test-token');
    assert.equal(seen[0].headers.authorization, undefined);
  });
});

const Result = z.object({
  name: z.string(),
  criteria: z.array(z.object({ id: z.string(), required: z.boolean() })),
});

test('nested objects sent as JSON strings are accepted', () => {
  const parsed = parseStructured(
    Result,
    JSON.stringify({
      name: 'n',
      criteria: JSON.stringify([{ id: 'c1', required: true }]),
    }),
  );
  assert.equal(parsed.problem, undefined);
  assert.equal((parsed.data as z.infer<typeof Result>).criteria[0].id, 'c1');
});

test('an invalid result names the fields to fix', () => {
  const parsed = parseStructured(
    Result,
    JSON.stringify({ criteria: [{ id: 1 }] }),
  );
  assert.equal(parsed.data, undefined);
  assert.match(parsed.problem ?? '', /name/);
  assert.match(parsed.problem ?? '', /criteria\.0\.required/);
  assert.match(parseStructured(Result, '{not json').problem ?? '', /not valid JSON/);
});

test('null spelled as text is decoded and remaining errors are reported', () => {
  const Analysis = z.object({
    hypothesis: z.string().nullable(),
    criteria: z.array(z.object({ id: z.string(), required: z.boolean() })),
  });
  const ok = parseStructured(
    Analysis,
    JSON.stringify({ hypothesis: 'None', criteria: JSON.stringify([{ id: 'c', required: true }]) }),
  );
  assert.equal(ok.problem, undefined);
  const bad = parseStructured(
    Analysis,
    JSON.stringify({ hypothesis: 'None', criteria: JSON.stringify([{ id: 'c' }]) }),
  );
  assert.match(bad.problem ?? '', /criteria\.0\.required/);
});
