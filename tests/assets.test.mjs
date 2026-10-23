import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const dist=new URL('../dist/',import.meta.url);
const digest=async file=>createHash('sha256').update(await readFile(new URL(file,dist))).digest('hex').slice(0,12);
for(const page of ['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html']){
  test(`${page} gives stylesheet and script changes a new browser cache key`,async()=>{
    const html=await readFile(new URL(page,dist),'utf8');
    const files=page==='docs/index.html'?['styles.css','theme.mjs','docs/docs.mjs']:['styles.css','theme.mjs','app.mjs',...(page==='build/index.html'?['sdk-flow.mjs']:[]),...(page==='pricing/index.html'?['pricing/pricing.css']:[])];
    for(const file of files)assert.ok(html.includes(`/${file}?v=${await digest(file)}`),`${file} must use its current content hash`);
  });
}
test('docs imports the same current app module as the homepage',async()=>{
  const docs=await readFile(new URL('docs/docs.mjs',dist),'utf8');
  assert.ok(docs.includes(`../app.mjs?v=${await digest('app.mjs')}`));
});
