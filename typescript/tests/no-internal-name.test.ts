// What the TypeScript SDK ships to developers must follow two public-surface rules.
//
// 1. Nothing may contain an internal name, including in hostnames and email
//    addresses. CoSens is the public product name.
//    These checks cover the files `npm pack` publishes (built dist/*.js and *.d.ts,
//    package.json, README and listed fixtures), the runtime exports of the package, the
//    HTTP requests its clients send, and the repository docs and examples published
//    alongside the package.
// 2. The SDK ships no provider-specific model client factory. Developers build the
//    standard `new OpenAI({ baseURL, apiKey })` for whichever provider they use and pass
//    it to `cosens(...)`. These checks cover the runtime exports, the published files,
//    and the published READMEs and examples.
//
// Not checked (not shipped to developers): tests/ other than files listed in
// package.json "files" (including this file), docs/testing/ validation records,
// typecheck/, and editor/CI/dev tooling files.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import * as sdk from '../src/index.ts';

// Internal names that must not reach developers, matched case-insensitively anywhere
// (identifiers, hostnames, emails, prose).
const INTERNAL_NAMES = new RegExp(['fri', 'day', '|', 'star', 'dust'].join(''), 'gi');
const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = join(PACKAGE_ROOT, '..');

// Exported names that would expose a model client factory, and published files that
// would hold a provider gateway module.
const MODEL_CLIENT_FACTORY_NAME = /gateway.?client|model.?client/i;
const GATEWAY_FILE_NAME = /gateway/i;

const PUBLISHED_DOCS = [
  join(PACKAGE_ROOT, 'README.md'),
  join(PACKAGE_ROOT, '.env.example'),
  ...readdirSync(join(PACKAGE_ROOT, 'examples')).sort().map(name => join(PACKAGE_ROOT, 'examples', name)),
];

// Published READMEs and examples that show how a model client is wrapped.
const CLIENT_DOCS = [
  join(REPO_ROOT, 'README.md'),
  join(PACKAGE_ROOT, 'README.md'),
  ...readdirSync(join(PACKAGE_ROOT, 'examples')).filter(name => name.endsWith('.ts')).sort().map(name => join(PACKAGE_ROOT, 'examples', name)),
];
// A model client is wrapped by passing it to cosens(...) or installing it as the
// OpenAI Agents default client.
const WRAPS_MODEL_CLIENT = /(?<![\w.])(?:cosens|setDefaultOpenAIClient)\(/;
const STANDARD_CONSTRUCTOR = /\bnew\s+OpenAI\(/g;
const FACTORY_CALL = /\w*gateway_?client\s*\(/i;

// Return the internal names found in `texts` as a diagnostic tag.
function matched(...texts: string[]): string {
  const names = [...new Set(texts.flatMap(text => [...text.matchAll(INTERNAL_NAMES)].map(match => match[0].toLowerCase())))].sort();
  return names.length ? `[${names.join(', ')}]` : '';
}

function textMatches(label: string, text: string): string[] {
  return text.split(/\r?\n/).flatMap((line, index) => matched(line) ? [`${label}:${index + 1}: ${matched(line)} ${line.trim()}`] : []);
}

function report(findings: string[], what: string): string {
  return `${findings.length} occurrence(s) of an internal name in ${what}:\n${findings.join('\n')}`;
}

// Every string in package.json, reported by its field path (author, maintainers,
// contributors, homepage, repository, bugs, email fields, and so on).
function jsonFieldMatches(label: string, value: unknown, path = ''): string[] {
  if (typeof value === 'string') return matched(value) ? [`${label} ${path || '(root)'}: ${matched(value)} ${value}`] : [];
  if (value === null || typeof value !== 'object') return [];
  return Object.entries(value).flatMap(([key, child]) => [
    ...(matched(key) ? [`${label} ${path ? `${path}.` : ''}${key}: ${matched(key)} field name`] : []),
    ...jsonFieldMatches(label, child, path ? `${path}.${key}` : key),
  ]);
}

function publishedFiles(): string[] {
  const output = execFileSync('npm', ['pack', '--dry-run', '--json', '--ignore-scripts'], { cwd: PACKAGE_ROOT, encoding: 'utf8' });
  const files: string[] = JSON.parse(output)[0].files.map((file: { path: string }) => file.path);
  assert.ok(files.includes('dist/index.js') && files.includes('dist/index.d.ts'), 'dist/ is missing from the package; run `npm run build` first');
  return files.sort();
}

test('files published to npm do not contain internal names', () => {
  const findings = publishedFiles().flatMap(path => [
    ...(matched(path) ? [`${path}: ${matched(path)} file path`] : []),
    ...textMatches(path, readFileSync(join(PACKAGE_ROOT, path), 'utf8')),
  ]);
  assert.deepEqual(findings, [], report(findings, 'the published npm package'));
});

test('package.json fields do not contain internal names', () => {
  const manifest = JSON.parse(readFileSync(join(PACKAGE_ROOT, 'package.json'), 'utf8'));
  const findings = jsonFieldMatches('package.json', manifest);
  assert.deepEqual(findings, [], report(findings, 'package.json fields'));
});

test('package exports do not contain internal names', () => {
  const findings = Object.keys(sdk).sort().filter(name => matched(name)).map(name => `export ${name} ${matched(name)}`);
  assert.deepEqual(findings, [], report(findings, 'runtime exports of the package'));
});

test('package exports have no model client factory', () => {
  const findings = Object.keys(sdk).sort().filter(name => MODEL_CLIENT_FACTORY_NAME.test(name)).map(name => `export ${name}`);
  assert.deepEqual(findings, [], `${findings.length} model client factory name(s) exported by the package; developers construct new OpenAI({ baseURL, apiKey }) themselves:\n${findings.join('\n')}`);
});

test('files published to npm include no gateway module', () => {
  const findings = publishedFiles().filter(path => GATEWAY_FILE_NAME.test(path.split('/').pop() ?? ''));
  assert.deepEqual(findings, [], `${findings.length} published file(s) named *gateway*; the SDK ships no provider gateway module:\n${findings.join('\n')}`);
});

function requestFindings(label: string, requests: Request[]): string[] {
  return requests.flatMap(request => [
    ...(matched(request.url) ? [`${label}: URL ${matched(request.url)} ${request.url}`] : []),
    ...[...request.headers].filter(([name, value]) => matched(name, value))
      .map(([name, value]) => `${label}: header ${matched(name, value)} ${name}: ${value}`),
  ]);
}

function recorder(seen: Request[]): typeof fetch {
  return async (input, init) => {
    seen.push(input instanceof Request ? input : new Request(input, init));
    return new Response(JSON.stringify({ object: 'list', data: [], task_id: 't' }), { headers: { 'Content-Type': 'application/json' } });
  };
}

for (const region of ['us'] as const) test(`${region} HTTP requests do not contain internal names`, async () => {
  const apiRequests: Request[] = [];
  const client = new sdk.CoSensClient({ apiKey: 'test-only-api-key', region, fetch: recorder(apiRequests) });
  await client.atoms.getTask('t');
  await client.close();
  assert.ok(apiRequests.length > 0, 'CoSensClient sent no request');
  const findings = requestFindings(`${region} CoSensClient (default base URL)`, apiRequests);

  assert.deepEqual(findings, [], report(findings, 'HTTP requests sent by the SDK'));
});

test('published docs and examples do not contain internal names', () => {
  const findings = PUBLISHED_DOCS.flatMap(path => {
    const label = relative(REPO_ROOT, path);
    return [...(matched(label) ? [`${label}: ${matched(label)} file path`] : []), ...textMatches(label, readFileSync(path, 'utf8'))];
  });
  assert.deepEqual(findings, [], report(findings, 'published TypeScript docs and examples'));
});

// Return the text between the parenthesis at `openParen` and its match.
function callArguments(text: string, openParen: number): string {
  let depth = 0;
  for (let index = openParen; index < text.length; index++) {
    depth += text[index] === '(' ? 1 : text[index] === ')' ? -1 : 0;
    if (depth === 0) return text.slice(openParen + 1, index);
  }
  return text.slice(openParen + 1);
}

function buildsStandardClient(text: string): boolean {
  return [...text.matchAll(STANDARD_CONSTRUCTOR)].some(match => {
    const args = callArguments(text, match.index + match[0].length - 1);
    return /\bbaseURL\b/.test(args) && /\bapiKey\b/.test(args);
  });
}

test('published docs and examples build model clients with the standard OpenAI constructor', () => {
  const findings = CLIENT_DOCS.flatMap(path => {
    const label = relative(REPO_ROOT, path);
    const text = readFileSync(path, 'utf8');
    const lines = text.split(/\r?\n/).map((line, index) => [index + 1, line] as const);
    return [
      ...lines.filter(([, line]) => FACTORY_CALL.test(line)).map(([number, line]) => `${label}:${number}: calls a model client factory: ${line.trim()}`),
      ...(buildsStandardClient(text) ? [] : lines.filter(([, line]) => WRAPS_MODEL_CLIENT.test(line))
        .map(([number, line]) => `${label}:${number}: wraps a model client, but the file never builds new OpenAI({ baseURL, apiKey }): ${line.trim()}`)),
    ];
  });
  assert.deepEqual(findings, [], `${findings.length} published README/example line(s) do not build the model client with the standard OpenAI constructor:\n${findings.join('\n')}`);
});
