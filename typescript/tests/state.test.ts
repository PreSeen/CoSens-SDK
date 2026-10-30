import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { openState, TurnStore } from '../src/state.js';

test('turn and recovery aliases commit together or not at all', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'cosens-state-')), 'state.sqlite3');
  const store = new TurnStore(path, 'key', 'service');
  const db = openState(path);
  db.exec("CREATE TRIGGER fail_alias BEFORE INSERT ON ts_turns WHEN NEW.key LIKE '%request:%' BEGIN SELECT RAISE(FAIL, 'alias write failed'); END");
  assert.throws(() => store.putMany(['turn-1', 'request:one'], { turnId: 'turn-1', state: 'pre_execution_pending' }), /alias write failed/);
  assert.equal(store.get('turn-1'), undefined);
  assert.equal(store.get('request:one'), undefined);
  db.exec('DROP TRIGGER fail_alias');
  store.putMany(['turn-1', 'request:one'], { turnId: 'turn-1', state: 'pre_execution_pending' });
  assert.deepEqual(store.get('turn-1'), store.get('request:one'));
  db.close();
  store.close();
});

test('workflow native dispatch claim is atomic across state store instances', () => {
  const path = join(mkdtempSync(join(tmpdir(), 'cosens-workflow-state-')), 'state.sqlite3');
  const first = new TurnStore(path, 'key', 'service');
  const second = new TurnStore(path, 'key', 'service');
  const pending = { turnId: 'turn-1', state: 'pre_execution_pending', taskSpec: { source: 'new_task' }, mode: 'auto' };
  assert.equal(first.insertIfAbsent('turn-1', pending), true);
  assert.equal(second.insertIfAbsent('turn-1', pending), false);
  const prepared = { ...pending, taskId: 't1', executionId: 'e1', requestSha256: 'h1' };
  assert.equal(second.updatePending('turn-1', prepared), true);
  assert.equal(first.updatePending('turn-1', pending), false);
  const running = { ...prepared, state: 'native_running' };
  assert.equal(first.claimState('turn-1', 'pre_execution_pending', running), true);
  assert.equal(second.claimState('turn-1', 'pre_execution_pending', running), false);
  assert.equal(second.updatePending('turn-1', prepared), false);
  assert.equal(first.get<{ state: string }>('turn-1')?.state, 'native_running');
  first.close(); second.close();
});
