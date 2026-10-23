import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
const read=p=>readFile(new URL('../dist/'+p,import.meta.url),'utf8');
test('home retains its concept and introduces three separate complete use sections',async()=>{
 const h=await read('index.html');
 assert.ok(h.includes('id="brain-diagram"'));
 for(const id of ['avatar','connect','developer'])assert.ok(h.includes(`id="${id}"`),id);
 assert.ok(h.indexOf('id="avatar"')<h.indexOf('id="connect"'));
 assert.ok(h.indexOf('id="connect"')<h.indexOf('id="developer"'));
 assert.ok(h.includes('href="/avatar/"'));
 assert.ok(h.includes('Request Avatar access'));
 assert.ok(h.includes('Request MCP access'));
 assert.ok(h.includes('Request SDK access'));
 assert.equal(h.includes('Three ways to put it to work.'),false);
});
test('all shells have approved navigation, access and login',async()=>{
 for(const page of ['index.html','avatar/index.html','build/index.html','pricing/index.html','docs/index.html','benchmarks/index.html','request-access/index.html']){
 const html=await read(page),nav=html.match(/<nav class="nav-links"[^>]*>([\s\S]*?)<\/nav>/)[1];
 assert.match(nav,/>Home<\/a>.*>Avatar<\/a>.*>Build<\/a>.*>Pricing<\/a>.*>Docs<\/a>/s);
 assert.ok(html.includes('Request access'));assert.ok(html.includes('Log in'));
 }
});
test('access form makes interest selection explicit and never claims a request was sent',async()=>{
 const html=await read('request-access/index.html');
 for(const value of ['avatar','mcp','sdk'])assert.ok(html.includes(`value="${value}"`));
 const js=await read('request-access/access.mjs');
 assert.ok(js.includes('not been sent'));assert.equal(js.includes('fetch('),false);
});

test('access preselection accepts only the three known interests and preserves honest status',async()=>{
 const {selectedInterest,previewMessage}=await import('../dist/request-access/access.mjs');
 assert.equal(selectedInterest('?interest=mcp'),'mcp');
 assert.equal(selectedInterest('?interest=avatar'),'avatar');
 assert.equal(selectedInterest('?interest=sdk'),'sdk');
 assert.equal(selectedInterest('?interest=other'),null);
 assert.equal(selectedInterest(''),null);
 assert.ok(previewMessage([]).includes('at least one'));
 assert.ok(previewMessage(['avatar','sdk']).includes('not been sent'));
});

test('home integrates first recall into the existing connection example',async()=>{
 const html=await read('index.html');
 assert.equal(html.includes('Try your first recall'),false);
 assert.ok(html.includes('class="journey-mini" id="first-recall"'));
 assert.ok(html.includes('Download the sample note'));
 assert.ok(html.includes('Start where you already built.'));
 const {homeJourneys,renderHomeJourney}=await import('../dist/home-journey.mjs');
 const rendered=renderHomeJourney(homeJourneys.connect[0]);
 assert.ok(rendered.includes('class="brand-sens">Sens</span>'));
});
