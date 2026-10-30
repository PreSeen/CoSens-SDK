import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CoSensClient, RSIHTTPError, RSITransportError } from '../src/index.ts';

const grant = { status: 'awaiting_upload', method: 'PUT',
  upload_url: 'https://friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/test_token',
  expires_at: '2099-01-01T00:00:00Z', max_bytes: 20 };

test('document grant and raw upload preserve binary bytes and gateway identity', async () => {
  const seen: Request[] = [];
  const c = new CoSensClient({ apiKey: 'document-key', region: 'cn', fetch: async (input, init) => {
    const request = new Request(String(input), init); seen.push(request);
    return new Response(JSON.stringify(request.url.endsWith('upload-grants') ? grant : { document_id: 'doc-1', status: 'processing' }));
  } });
  try {
    const issued = await c.atoms.createDocumentUploadGrant({ filename: 'sample.bin', mimeType: 'application/octet-stream', threadId: 'thread-1', waitForProcessing: false });
    const bytes = new Uint8Array([0, 255, 1, 13, 10]);
    const receipt = await c.atoms.uploadDocumentToGrant(issued, bytes);
    assert.equal(receipt.document_id, 'doc-1'); assert.equal(receipt.status, 'processing');
    await c.atoms.getDocumentStatus([receipt.document_id]);
    assert.deepEqual(seen.map(r => [r.method, new URL(r.url).pathname]), [
      ['POST', '/api/v1/document/upload-grants'], ['PUT', '/api/v1/document/uploads/test_token'], ['POST', '/api/v1/document/status']]);
    assert.deepEqual(await seen[0]!.json(), { filename: 'sample.bin', mime_type: 'application/octet-stream', thread_id: 'thread-1', wait_for_processing: false });
    assert.deepEqual(new Uint8Array(await seen[1]!.arrayBuffer()), bytes);
    assert.equal(seen[1]!.headers.get('content-type'), 'application/octet-stream');
    assert.deepEqual(await seen[2]!.json(), { document_ids: ['doc-1'] });
    assert.ok(seen.every(r => r.headers.get('authorization') === 'Bearer document-key' && r.headers.get('x-auth-type') === 'api_key' && !r.headers.has('x-user-id')));
  } finally { await c.close(); }
});

test('ordinary document upload uses multipart with unmodified bytes and explicit metadata', async () => {
  let request: Request | undefined;
  const c = new CoSensClient({ apiKey: 'document-key', fetch: async (input, init) => { request = new Request(String(input), init); return new Response('{"document_id":"doc-1"}'); } });
  try {
    const bytes = Buffer.from('hello\x00world');
    await c.atoms.uploadDocument({ filename: 'sample.txt', mimeType: 'text/plain', content: bytes, ingestMode: 'fulltext', waitForProcessing: false });
    assert.equal(new URL(request!.url).pathname, '/api/v1/document/upload');
    const form = await request!.formData();
    const file = form.get('file') as File;
    assert.equal(file.name, 'sample.txt'); assert.equal(file.type, 'text/plain');
    assert.deepEqual(Buffer.from(await file.arrayBuffer()), bytes);
    assert.equal(form.get('ingest_mode'), 'fulltext'); assert.equal(form.get('wait_for_processing'), 'false');
  } finally { await c.close(); }
});

test('upload grant rejects foreign origins, traversal and URL credentials before sending', async () => {
  let calls = 0;
  const c = new CoSensClient({ apiKey: 'document-key', region: 'cn', fetch: async () => { calls++; return new Response('{}'); } });
  try {
    for (const upload_url of [
      'https://attacker.example/api/v1/document/uploads/token', 'https://rsi.cosens.ai/api/v1/document/uploads/token',
      'http://friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token',
      'https://user:password@friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token',
      'https://@friday-rsi-cn.stardust-ai.com/api/v1/document/uploads/token',
      'https://friday-rsi-cn.stardust-ai.com:444/api/v1/document/uploads/token',
      '/v1/document/uploads/../token', '/v1/document/uploads/%2e%2e', '/v1/document/uploads/token?x=y',
      '/v1/document/uploads/token#fragment', '/v1/document/uploads/a/b', '/v1/other/token', '//attacker.example/v1/document/uploads/token',
    ]) await assert.rejects(async () => c.atoms.uploadDocumentToGrant({ ...grant, upload_url }, Buffer.from('data')), /invalid document upload target/);
    assert.equal(calls, 0);
  } finally { await c.close(); }
});

test('relative upload paths preserve prefix and ignore untrusted grant instructions', async () => {
  for (const upload_url of ['/v1/document/uploads/test_token', '/api/v1/document/uploads/test_token']) {
    const seen: Request[] = [];
    const c = new CoSensClient({ apiKey: 'document-key', region: 'cn', fetch: async (input, init) => {
      seen.push(new Request(String(input), init));
      return new Response('{"ok":false,"status":"failed","failure_kind":"unsupported_format"}');
    } });
    try {
      const receipt = await c.atoms.uploadDocumentToGrant({ upload_url, method: 'PUT', headers: { Authorization: 'untrusted' }, curl: 'untrusted instruction' }, Buffer.from('data'));
      assert.equal(receipt.ok, false); assert.equal(receipt.status, 'failed');
      assert.equal(seen.length, 1); assert.equal(new URL(seen[0]!.url).pathname, '/api/v1/document/uploads/test_token');
      assert.equal(seen[0]!.headers.get('authorization'), 'Bearer document-key');
    } finally { await c.close(); }
  }
});

test('document metadata cannot override gateway identity and status requires IDs', async () => {
  let calls = 0;
  const c = new CoSensClient({ apiKey: 'document-key', fetch: async () => { calls++; return new Response('{}'); } });
  try {
    for (const extra of [{ user_id: 'other' }, { graph_id: 'other' }, { waitTimeoutSeconds: 0 }, { waitForProcessing: 'false' }]) {
      await assert.rejects(async () => c.atoms.createDocumentUploadGrant({ filename: 'a.txt', mimeType: 'text/plain', ...extra } as any), TypeError);
    }
    await assert.rejects(async () => c.atoms.getDocumentStatus([]), /documentIds/);
    assert.equal(calls, 0);
  } finally { await c.close(); }
});

test('upload preflight validates method expiry size and content without consuming the grant', async () => {
  let calls = 0;
  const c = new CoSensClient({ apiKey: 'document-key', fetch: async () => { calls++; return new Response('{}'); } });
  try {
    for (const [value, content] of [
      [{ ...grant, method: 'POST' }, Buffer.from('x')], [{ ...grant, expires_at: '2000-01-01T00:00:00Z' }, Buffer.from('x')],
      [{ ...grant, max_bytes: 1 }, Buffer.from('large')], [grant, Buffer.alloc(0)], [grant, 'not bytes'],
    ] as const) await assert.rejects(async () => c.atoms.uploadDocumentToGrant(value, content as Uint8Array), /upload|content|bytes/i);
    assert.equal(calls, 0);
  } finally { await c.close(); }
});

test('one-time upload never retries redirects or failures and redacts credentials', async () => {
  for (const failure of ['redirect', 'network', 'http']) {
    let calls = 0;
    const c = new CoSensClient({ apiKey: 'document-key', fetch: async () => {
      calls++;
      if (failure === 'network') throw new Error('test_token document-key');
      if (failure === 'redirect') return new Response(null, { status: 307, headers: { location: 'https://attacker.example/upload' } });
      return new Response('{"error":{"message":"test_token document-key"}}', { status: 500 });
    } });
    try {
      await assert.rejects(c.atoms.uploadDocumentToGrant(grant, Buffer.from('data')), (error: any) => {
        assert.ok(error instanceof RSIHTTPError || error instanceof RSITransportError);
        assert.ok(!String(error).includes('test_token') && !String(error).includes('document-key'));
        return true;
      });
      assert.equal(calls, 1);
    } finally { await c.close(); }
  }
});
