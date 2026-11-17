import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { pathToFileURL } from 'node:url';
import { CursorStore, TurnStore, SessionHistoryChanged } from '../src/index.ts';
import { canonical } from '../src/state.ts';

const session = (items: any[]) => ({ sessionId: 's1', getItems: async () => items });
const file = () => join(mkdtempSync(join(tmpdir(), 'cosens-ts-')), 'state.sqlite3');

test('state canonicalization uses stable UTF-8 byte key ordering', () => {
  assert.equal(canonical({ b: 1, ä: 4, A: 2, a: 3 }), '{"A":2,"a":3,"b":1,"ä":4}');
  assert.equal(canonical({ '2': 'two', '10': 'ten' }), '{"10":"ten","2":"two"}');
});

test('duplicate Session message IDs are rejected before a batch can be sent', async () => {
  const store = new CursorStore(file(), 'principal', 'service');
  await assert.rejects(store.snapshot(session([
    { id: 'same', role: 'user', content: 'first' },
    { id: 'same', role: 'assistant', content: 'second' },
  ])), SessionHistoryChanged);
  store.close();
});

test('turn records persist without storing the principal literal', () => {
  const path = file();
  const store = new TurnStore(path, 'secret-key', 'service');
  store.put('turn-1', { state: 'pre_execution_pending' });
  store.close();
  const reopened = new TurnStore(path, 'secret-key', 'service');
  assert.equal(reopened.get('turn-1')?.state, 'pre_execution_pending');
  reopened.close();
});

test('Session lease waits across processes and recovers after a process exits', async () => {
  const path = file();
  const timeout = (message: string) => new Promise<never>((_, reject) => {
    const timer = setTimeout(() => reject(Error(message)), 5_000);
    timer.unref();
  });
  const moduleUrl = pathToFileURL(join(process.cwd(), 'src/cursor.ts')).href;
  const script = `import { CursorStore } from ${JSON.stringify(moduleUrl)};
    const store = new CursorStore(process.argv[1], 'principal', 'service');
    await store.acquireSession({ sessionId: 's1', getItems: async () => [] });
    process.stdout.write('READY\\n');
    setInterval(() => {}, 1_000);`;
  const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', script, path], { cwd: process.cwd(), stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    const ready = Promise.race([
      once(child.stdout, 'data').then(([chunk]) => String(chunk)),
      once(child, 'exit').then(([code]) => { throw Error(`lock holder exited early: ${code}`); }),
      timeout('lock holder did not start'),
    ]);
    assert.match(await ready, /READY/);
    const store = new CursorStore(path, 'principal', 'service');
    let acquired = false;
    const waiting = store.acquireSession(session([])).then(release => { acquired = true; return release; });
    await new Promise(resolve => setTimeout(resolve, 75));
    assert.equal(acquired, false);
    child.kill('SIGKILL');
    await once(child, 'exit');
    const release = await Promise.race([waiting, timeout('stale lock was not reclaimed')]);
    assert.equal(acquired, true);
    release(); store.close();
  } finally { if (child.exitCode === null) child.kill('SIGKILL'); }
});
