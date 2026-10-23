import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const read=file=>readFile(new URL('../dist/'+file,import.meta.url),'utf8');
const registry=async()=>{const text=await read('docs/docs.mjs');return runInNewContext(text.slice(text.indexOf('const code='),text.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');};

test('first-use walkthrough has a source-checkable sample and matching recall prompt',async()=>{
 const home=await read('index.html');
 const pages=await registry();
 const sample=await read('assets/first-memory.md');
 const body=pages.connect.sections.flat().join(' ');
 assert.ok(home.includes('id="first-recall"'));
 assert.ok(home.includes('href="/assets/first-memory.md"'));
 assert.ok(body.includes('/assets/first-memory.md'));
 assert.ok(sample.includes('October 26, 2026'));
 assert.ok(sample.includes('security review'));
 assert.ok(body.includes('What did we decide about the pilot launch, and what still needs approval?'));
 assert.ok(body.includes('October 26'));
 assert.ok(body.includes('security review'));
 assert.ok(body.includes('same CoSens account'));
 assert.ok(body.includes('read-only'));
 assert.ok(body.includes('https://memory.preseen.ai/ingest'));
 const order=pages.connect.sections.map(section=>section[0]);
 assert.ok(order.indexOf('connect')<order.indexOf('bring'));
 assert.ok(order.indexOf('bring')<order.indexOf('ask'));
});

test('preview actions and identity are explained at the point of entry',async()=>{
 const home=await read('index.html');
 const build=await read('build/index.html');
 assert.ok(home.includes('href="/docs/#learn">Learn in preview'));
 assert.ok(build.includes('Explore SDK preview'));
 assert.ok(build.includes('Package and setup instructions are coming with the SDK release'));
 const pages=await registry();
 for(const key of ['connect','chatgpt','claude','codex','claude-code']){
  const body=pages[key].sections.flat().join(' ');
  assert.ok(body.includes('Friday Memory'),key);
  assert.ok(body.includes('memory.preseen.ai'),key);
  assert.ok(body.includes('keep the installer-provided name'),key);
 }
 assert.ok(pages.quickstart.notice.includes('not an installable SDK release'));
});

test('Build benchmark summary links each selected row to its source details',async()=>{
 const html=await read('build/index.html');
 const report=JSON.parse(await read('benchmarks/results.json'));
 for(const name of report.homepage.benchmarkNames){
  const row=report.benchmarks.find(item=>item.name===name);
  assert.ok(html.includes(`/benchmarks/#benchmark-${row.id}`),name);
 }
});
