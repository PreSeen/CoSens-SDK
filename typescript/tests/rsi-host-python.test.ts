import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { resolveHostPython } from '../src/rsi/host-python.js';

test('managed Python rejects altered archives and cleans up the partial installation', {
  skip: !['darwin', 'linux', 'win32'].includes(process.platform) ||
    !['arm64', 'x64'].includes(process.arch),
}, async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rsi-python-install-'));
  const pathKey = Object.keys(process.env).find((key) => key.toLowerCase() === 'path') ?? 'PATH';
  const previousPath = process.env[pathKey];
  process.env[pathKey] = root;
  t.after(async () => {
    if (previousPath === undefined) delete process.env[pathKey];
    else process.env[pathKey] = previousPath;
    await rm(root, { recursive: true, force: true });
  });
  const fetch = t.mock.method(globalThis, 'fetch', async (url: string) => {
    assert.match(url, /^https:\/\/github\.com\/astral-sh\/python-build-standalone\/releases\/download\//);
    return new Response('corrupted Python archive');
  });

  await assert.rejects(resolveHostPython(root, () => {}), /SHA-256/);
  assert.equal(fetch.mock.callCount(), 1);
  assert.deepEqual(await readdir(join(root, 'runtimes')), []);
});
