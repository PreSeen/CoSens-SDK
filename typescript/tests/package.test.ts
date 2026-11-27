// What `npm publish` ships: the file list, internal names and both module systems.
// Needs a built dist/ (`npm run check` builds first).
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, sep } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const INTERNAL_NAMES = new RegExp('fri' + 'day|star' + 'dust', 'i');

let packed: string[] | undefined;

/**
 * The files `npm pack` selects. npm runs the `prepare` build on pack even with
 * --ignore-scripts, which would replace dist/ under the other test files, so
 * this packs a copy of the package without its scripts.
 */
function packedFiles(): string[] {
  if (packed) return packed;
  const copy = mkdtempSync(join(tmpdir(), 'cosens-sdk-pack-'));
  try {
    cpSync(root, copy, {
      recursive: true,
      filter: (source) => !source.split(sep).includes('node_modules'),
    });
    const manifest = JSON.parse(readFileSync(join(copy, 'package.json'), 'utf8'));
    delete manifest.scripts;
    writeFileSync(join(copy, 'package.json'), JSON.stringify(manifest, null, 2));
    const output = execFileSync('npm', ['pack', '--dry-run', '--json'], {
      cwd: copy,
      encoding: 'utf8',
    });
    const [pack] = JSON.parse(output) as { files: { path: string }[] }[];
    packed = pack.files.map((file) => file.path).sort();
    return packed;
  } finally {
    rmSync(copy, { recursive: true, force: true });
  }
}

test('the package ships only dist, README, LICENSE, NOTICE and package.json', () => {
  const files = packedFiles();
  assert.ok(files.includes('dist/index.js'), 'dist/ is missing; run npm run build');
  for (const required of [
    'LICENSE',
    'NOTICE',
    'README.md',
    'package.json',
    'dist/index.d.ts',
    'dist/memory.js',
    'dist/rsi/index.js',
    'dist/rsi/worker.js',
  ])
    assert.ok(files.includes(required), `${required} is not packed`);
  const unexpected = files.filter(
    (path) =>
      !['LICENSE', 'NOTICE', 'README.md', 'package.json'].includes(path) &&
      !/^dist\/.+\.(js|d\.ts)$/.test(path),
  );
  assert.deepEqual(unexpected, []);
});

test('packed files contain no internal names', () => {
  const hits = packedFiles().filter(
    (path) =>
      INTERNAL_NAMES.test(path) ||
      INTERNAL_NAMES.test(readFileSync(join(root, path), 'utf8')),
  );
  assert.deepEqual(hits, []);
});

test('the package loads with import and with require', () => {
  const names = (type: 'module' | 'commonjs', code: string) =>
    JSON.parse(
      execFileSync(process.execPath, ['--input-type', type, '-e', code], {
        cwd: root,
        encoding: 'utf8',
        env: { ...process.env, NODE_OPTIONS: '' },
      }),
    ) as string[];
  const esm = names(
    'module',
    `import * as sdk from 'cosens-sdk'; import * as rsi from 'cosens-sdk/rsi'; import * as memory from 'cosens-sdk/memory';
     console.log(JSON.stringify([...Object.keys(sdk), ...Object.keys(rsi).map((k) => 'rsi.' + k), ...Object.keys(memory).map((k) => 'memory.' + k)].sort()));`,
  );
  const cjs = names(
    'commonjs',
    `const sdk = require('cosens-sdk'), rsi = require('cosens-sdk/rsi'), memory = require('cosens-sdk/memory');
     const keys = (m) => Object.keys(m).filter((k) => k !== '__esModule' && k !== 'module.exports');
     console.log(JSON.stringify([...keys(sdk), ...keys(rsi).map((k) => 'rsi.' + k), ...keys(memory).map((k) => 'memory.' + k)].sort()));`,
  );
  assert.deepEqual(cjs, esm);
  for (const name of ['RSIClient', 'RSIError', 'memoryRecall', 'memoryIngest', 'rsi.RSIClient', 'memory.getMemoryJob'])
    assert.ok(esm.includes(name), `${name} is not exported`);
});
