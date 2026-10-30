import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

for (const file of ['01_transparent.ts', '02_explicit.ts', '03_atomic.ts']) {
  test(`${file} validates its Friday Key before making a request`, () => {
    const result = spawnSync(process.execPath, ['--import', 'tsx', `examples/${file}`], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 10_000,
      env: { ...process.env, RSI_API_KEY: '', COSENS_API_KEY: '', OPENAI_API_KEY: '', OPENAI_MODEL: '' },
    });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /RSI_API_KEY/);
    assert.doesNotMatch(result.stderr, /ERR_MODULE_NOT_FOUND|Cannot find module/);
  });
}
