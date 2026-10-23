import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const read=name=>readFile(new URL('../dist/'+name,import.meta.url),'utf8');
const shells=['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html'];

test('Pricing is reachable from every header and footer and has the correct current state',async()=>{
 for(const file of shells){
  const html=await read(file),header=html.match(/<header[\s\S]*?<\/header>/)[0],footer=html.match(/<footer[\s\S]*?<\/footer>/)[0];
  assert.match(header,/<a href="\/pricing\/"[^>]*>Pricing<\/a>/);
  assert.match(footer,/href="\/pricing\/"/);
  assert.equal(header.includes('href="/pricing/" aria-current="page"'),file==='pricing/index.html');
 }
});
test('Pricing preserves the confirmed plans and one credit balance for processing and execution',async()=>{
 const html=await read('pricing/index.html');
 for(const [name,price,storage,credit,count] of [['Free','0','100 MB','1','3'],['Plus','9.9','1 GB','10','10'],['Pro','39.9','5 GB','40','50']]){
  const card=html.match(new RegExp(`<article class="pricing-card[^>]*data-plan="${name.toLowerCase()}"[\\s\\S]*?<\\/article>`))[0];
  for(const text of [`>${name}</h2>`,`$${price}`,`${storage} of experience storage`,`$${credit} for experience processing and agent execution`,`${count} custom Recipes`])assert.ok(card.includes(text),text);
  assert.ok(card.includes('Included each month · one shared balance'));
 }
 assert.ok(html.includes('stored content and graph data'));
 assert.ok(html.includes('Versions do not count as additional Recipes.'));
 assert.ok(html.includes('Experience processing includes memory processing and Recipe iteration.'));
 assert.ok(html.includes('Learn, the SDK and Recipe management are in preview.'));
 for(const text of ['内部评审','0.5','5,000','aria-disabled','<button','file storage','market price','70%'])assert.equal(html.includes(text),false,text);
});
test('Pricing CTAs provide real connection and plan-specific email handoffs without checkout claims',async()=>{
 const html=await read('pricing/index.html');
 assert.match(html,/href="\/docs\/#connect"[^>]*>Start free/);
 const emails=[...html.matchAll(/href="(mailto:[^"]+)"/g)].map(m=>m[1]);
 assert.equal(emails.length,3);
 for(const email of emails){const url=new URL(email);assert.equal(url.pathname,'hello@cosens.ai');assert.ok(url.searchParams.get('subject'));}
 assert.ok(emails.some(x=>decodeURIComponent(x).includes('Plus')));
 assert.ok(emails.some(x=>decodeURIComponent(x).includes('Pro')));
 assert.ok(html.includes('Email us to arrange your plan.'));
 assert.ok(html.includes('does not process subscription payments or top-ups'));
});
test('Docs renders the same plan entitlements and states storage, model and Recipe boundaries',async()=>{
 const source=await read('docs/docs.mjs');
 const pages=runInNewContext(source.slice(source.indexOf('const code='),source.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');
 const article=pages.pricing.sections.flat().join(' ');
 for(const row of ['<td>Free</td><td>$0</td><td>100 MB</td><td>$1</td><td>3</td>','<td>Plus</td><td>$9.9</td><td>1 GB</td><td>$10</td><td>10</td>','<td>Pro</td><td>$39.9</td><td>5 GB</td><td>$40</td><td>50</td>'])assert.ok(article.includes(row));
 for(const text of ['graph data count toward experience storage','Versions do not count as additional Recipes.','experience processing and agent execution','directly to your own model-provider account','mailto:hello@cosens.ai','href="/pricing/"','class="pricing-doc-table"'])assert.ok(article.includes(text),text);
});
test('Pricing preserves its contact HTML at the edge instead of relying on email rewrite scripts',async()=>{
 const headers=await read('_headers');
 for(const route of ['/pricing/','/pricing/index.html']){
  const rule=headers.split('\n\n').find(block=>block.split('\n')[0]===route);
  assert.ok(rule,route);
  assert.match(rule,/Cache-Control: public, max-age=0, must-revalidate, no-transform/);
 }
});
