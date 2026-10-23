import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const read=file=>readFile(new URL('../dist/'+file,import.meta.url),'utf8');

test('Connect is the default page and developer integration lives at Build',async()=>{
 const home=await read('index.html');
 assert.match(home,/<title>CoSens — Give your AI a second brain\.<\/title>/);
 for(const text of ['Sensable AI.','Better at your kind of work.','Your experience,'])assert.ok(home.replace(/<[^>]+>/g,' ' ).replace(/\s+/g,' ').includes(text),text);
 assert.ok(home.includes('id="demo-panel"'));
 assert.equal(home.includes('benchmark-table'),false);
 assert.ok(home.includes('data-code="wrapper"'));
 const build=await read('build/index.html');
 assert.ok(build.includes('https://cosens.ai/build/'));
 assert.ok(build.includes('data-code="wrapper"'));
 assert.ok(build.includes('id="sdk-workflow"'));
 assert.ok(build.includes('benchmark-table'));
});

test('all seven public shells use ordered standalone destinations',async()=>{
 for(const path of ['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html']){
  const html=await read(path);
  const nav=html.match(/<nav class="nav-links"[^>]*>(.*?)<\/nav>/s)[1];
  assert.match(nav,/href="\/"[^>]*>Home<\/a>.*href="\/avatar\/"[^>]*>Avatar<\/a>.*href="\/build\/"[^>]*>Build<\/a>.*href="\/docs\/"[^>]*>Docs<\/a>/s);
  assert.equal(html.includes('href="/#use"'),false);
 }
});

test('Connect uses a personal dinner scenario and preserves capability boundaries',async()=>{
 const home=await read('index.html');
 assert.ok(home.includes('A more useful next plan.'));
 for(const id of ['0','1','2','3'])assert.ok(home.includes('data-step="'+id+'"'));
 assert.ok(home.includes('App connections, including Gmail, are coming soon'));
 assert.ok(home.includes('SCRIPTED RESPONSES'));
 assert.ok(home.includes('complete Learn loop is in preview'));
});

test('Docs retains every connection and developer article in its sidebar',async()=>{
 const html=await read('docs/index.html');
 const destinations=[...html.matchAll(/class="doc-nav-link" href="#([^"]+)"/g)].map(match=>match[1]);
 assert.deepEqual(destinations,['connect','import','apps','chatgpt','claude','codex','claude-code','mcp','overview','quickstart','learn','validation','api','memory','recipes','feedback','pricing']);
});

test('public page links resolve after splitting Connect and Build',async()=>{
 const routes=new Map();
 for(const [path,file]of [['/','index.html'],['/build/','build/index.html'],['/docs/','docs/index.html'],['/benchmarks/','benchmarks/index.html'],['/pricing/','pricing/index.html'],['/avatar/','avatar/index.html'],['/request-access/','request-access/index.html']])routes.set(path,await read(file));
 const docs=await read('docs/docs.mjs');
 const articles=runInNewContext(docs.slice(docs.indexOf('const code='),docs.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');
 const docsBody=Object.values(articles).map(page=>page.sections.flat().join(' ')).join(' ');
 for(const [path,html]of [...routes,['/docs/',docsBody]]){
  for(const [,href]of html.matchAll(/href="([^"]+)"/g)){
   const url=new URL(href,'https://cosens.ai'+path);
   if(url.origin!=='https://cosens.ai'||!url.pathname.endsWith('/'))continue;
   assert.ok(routes.has(url.pathname),`${path}: ${href}`);
   if(!url.hash)continue;
   if(url.pathname==='/docs/')assert.ok(articles[url.hash.slice(1)]||routes.get(url.pathname).includes(`id="${url.hash.slice(1)}"`),`${path}: ${href}`);
   else assert.ok(routes.get(url.pathname).includes(`id="${url.hash.slice(1)}"`),`${path}: ${href}`);
  }
 }
});
