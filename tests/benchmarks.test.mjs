import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const dist=new URL('../dist/',import.meta.url);
const baseScores=b=>Object.fromEntries(['cosens','mem0','tencent'].map(id=>[id,b.scores[id]]));
const report=JSON.parse(await readFile(new URL('benchmarks/results.json',dist),'utf8'));
test('benchmark report preserves selected source scores and missing results',()=>{
 assert.deepEqual(report.benchmarks.map(b=>b.name),['LoCoMo','HaluMem','MemoryAgentBench','OrgMemBench','LongMemEval','LoCoMo-Plus','MemBench']);
 assert.deepEqual(baseScores(report.benchmarks[0]),{cosens:0.368945,mem0:0.256981,tencent:0.186029});
 assert.deepEqual(baseScores(report.benchmarks[1]),{cosens:0.537369,mem0:0.492279,tencent:0.519456});
 assert.equal(report.benchmarks[1].metric,'Medium QA Correct');
 assert.deepEqual(baseScores(report.benchmarks[2]),{cosens:null,mem0:0.035893,tencent:0.038262});
 assert.deepEqual(baseScores(report.benchmarks[3]),{cosens:0.3241,mem0:null,tencent:null});
});
for(const file of ['build/index.html','benchmarks/index.html']){
 test(`${file} renders its intended comparison without changing the source scores`,async()=>{
  const compact=file==='build/index.html';
  const html=await readFile(new URL(file,dist),'utf8');
  const table=html.slice(html.indexOf('<table class="benchmark-table">'),html.indexOf('</table>')+8);
  const head=table.slice(table.indexOf('<thead>'),table.indexOf('</thead>'));
  const headers=[...head.matchAll(/<th[^>]*>(.*?)<\/th>/g)].map(m=>m[1].replace(/<[^>]*>/g,''));
  assert.deepEqual(headers,compact?['Benchmark','CoSens','Mem0','A-Mem','MemGPT']:['Benchmark','CoSens','Mem0','TencentDB Agent Memory','A-Mem','SeCom','MemGPT','RAG (local)','Session RAG (V+fact)']);
  const body=table.slice(table.indexOf('<tbody>'),table.indexOf('</tbody>'));
  const names=[...body.matchAll(/<th scope="row">([^<]+)/g)].map(m=>m[1]);
  assert.deepEqual(names,compact?['LoCoMo','LoCoMo-Plus','HaluMem','LongMemEval']:report.benchmarks.map(b=>b.name));
  assert.equal([...body.matchAll(/>—<\/td>/g)].length,compact?8:38);
  for(const score of ['0.368945','0.256981','61.52','59.64','0.537369','95.2'])assert.ok(body.includes(score));
  assert.equal(html.includes('Composite rank'),false);
  assert.ok(html.includes(report.disclaimer));
  if(compact)assert.ok(html.includes('href="/benchmarks/">More benchmark...'));
 });
}

test('benchmark metadata covers the displayed benchmarks without a ranking claim',async()=>{
 const html=await readFile(new URL('benchmarks/index.html',dist),'utf8');
 const meta=html.match(/<meta name="description" content="([^"]+)"/)[1];
 assert.ok(meta.includes('LoCoMo'));assert.ok(meta.includes('CoSens'));
 assert.ok(meta.includes('HaluMem'));assert.equal(meta.includes('rank'),false);
});
test('downloaded score data has a content-versioned URL',async()=>{
 const data=await readFile(new URL('benchmarks/results.json',dist));
 const hash=createHash('sha256').update(data).digest('hex').slice(0,12);
 assert.ok((await readFile(new URL('benchmarks/index.html',dist),'utf8')).includes(`/benchmarks/results.json?v=${hash}`));
});

test('additional report references preserve metric scales and source distinctions',()=>{
 const long=report.benchmarks.find(b=>b.name==='LongMemEval');assert.equal(long.scores.cosens,95.2);assert.equal(long.scores.sessionrag,86.2);assert.ok(long.metric.includes('Recall@10'));
 const plus=report.benchmarks.find(b=>b.name==='LoCoMo-Plus');assert.equal(plus.scores.cosens,61.52);assert.equal(plus.scores.amem,59.64);assert.equal(plus.scores.mem0,57.24);assert.equal(plus.scores.secom,57.53);
 const mem=report.benchmarks.find(b=>b.name==='MemBench');assert.equal(mem.scores.cosens,.892);assert.equal(mem.scores.memgpt,.455);assert.equal(mem.scores.raglocal,.722);
 assert.ok(report.configuration.includes('not a controlled'));
});

test('every benchmark has traceable methodology, public resources and score provenance',async()=>{
 const html=await readFile(new URL('benchmarks/index.html',dist),'utf8');
 for(const b of report.benchmarks){
  assert.ok(b.id,`${b.name}: stable detail anchor`);
  assert.ok(b.links?.length>=2,`${b.name}: benchmark and evaluation resources`);
  for(const link of b.links)assert.ok(new URL(link.url).protocol==='https:');
  for(const key of ['purpose','dataset','procedure','scoring','scope'])assert.ok(b.methodology?.[key]?.length>30,`${b.name}: ${key}`);
  assert.ok(report.sources[b.sourceId],`${b.name}: source report`);
  for(const [system,score] of Object.entries(b.scores))if(score!==null)assert.ok(['company-run','paper-reference','local-baseline'].includes(b.scoreOrigins[system]),`${b.name}/${system}: provenance`);
  assert.ok(html.includes(`id="benchmark-${b.id}"`));
  for(const link of b.links)assert.ok(html.includes(`href="${link.url}"`));
 }
 assert.ok(html.includes('MemoryAgentBench uses an adapted Token F1'));
 assert.ok(html.includes('not end-to-end answer accuracy'));
});
test('every compared system has its own address or explicitly identified baseline source',async()=>{
 const html=await readFile(new URL('benchmarks/index.html',dist),'utf8');
 for(const system of report.systems){
  assert.ok(system.links?.length,`${system.name}: resources`);
  assert.ok(system.description?.length>30);
  assert.ok(system.evaluationNote?.length>30);
  assert.ok(html.includes(`id="system-${system.id}"`));
  for(const link of system.links)assert.ok(html.includes(`href="${link.url}"`));
 }
 for(const source of Object.values(report.sources))assert.ok(html.includes(source.url));
 assert.ok(html.includes('Exact evaluated versions are not provided'));
 assert.ok(html.includes('Sample IDs and counts'));
});
