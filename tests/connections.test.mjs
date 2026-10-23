import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';
const dist=new URL('../dist/',import.meta.url);
test('all public page headers provide Home, Avatar, Build and Docs destinations',async()=>{
 for(const path of ['index.html','build/index.html','docs/index.html','benchmarks/index.html']){
  const html=await readFile(new URL(path,dist),'utf8');
  const nav=html.match(/<nav class="nav-links"[^>]*>(.*?)<\/nav>/s)[1];
  assert.match(nav,/href="\/build\/"[^>]*>Build<\/a>/);
  assert.match(nav,/href="\/"[^>]*>Home<\/a>/);
  assert.match(nav,/href="\/docs\/"[^>]*>Docs<\/a>/);
 }
});
test('homepage Connect content connects to setup guides without pretending Gmail is connected',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 assert.match(html,/id="connect"/);
 assert.match(html,/Give your AI<\/span><span class="headline-accent">a second brain\./);
 for(const guide of ['chatgpt','claude','codex','claude-code'])assert.ok(html.includes(`/docs/#${guide}`));
 assert.match(html,/App connections, including Gmail, are coming soon/);
 assert.match(html,/SCRIPTED RESPONSES/);
 assert.match(html,/id="demo-panel"/);
 assert.equal(/\bfake\b/i.test(html),false);
});
test('MCP and plugin guides keep executable setup separate from SDK preview',async()=>{
 const docs=await readFile(new URL('docs/docs.mjs',dist),'utf8');
 const index=await readFile(new URL('docs/index.html',dist),'utf8');
 for(const guide of ['connect','import','apps','chatgpt','claude','codex','claude-code','mcp'])assert.ok(index.includes(`href="#${guide}"`));
 assert.ok(docs.includes('https://memory.preseen.ai/mcp/'));
 const registry=runInNewContext(docs.slice(docs.indexOf('const code='),docs.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');
 assert.ok(registry.codex.sections.flat().join(' ').includes('--target codex'));
 assert.ok(registry['claude-code'].sections.flat().join(' ').includes('--target claude'));
 for(const key of ['connect','import','apps','chatgpt','claude','codex','claude-code','mcp']){assert.ok(registry[key].badge);assert.ok(registry[key].notice);}
 const known=new Set(Object.keys(registry));
 for(const page of Object.values(registry))for(const match of page.sections.flat().join(' ').matchAll(/href="#([^"]+)"/g))assert.ok(known.has(match[1]),'Unknown guide '+match[1]);
 assert.ok(docs.includes('page.notice'));assert.ok(docs.includes('page.badge'));
 assert.ok(docs.includes('data-copy-text'));assert.ok(docs.includes('copyExample'));
 assert.ok(docs.includes('Gmail synchronization is not available from this website'));
});

test('usage guides follow the current host-file and shell-upload contract',async()=>{
 const text=await readFile(new URL('docs/docs.mjs',dist),'utf8');
 const pages=runInNewContext(text.slice(text.indexOf('const code='),text.indexOf('const content=document')).replace('export const pages=','const pages=')+'; pages;');
 const body=key=>pages[key].sections.flat().join(' ');
 assert.ok(body('chatgpt').includes('attached file'));
 assert.ok(body('import').includes('one-time upload URL'));
 assert.ok(body('codex').includes('upload command returned by'));
 assert.ok(body('claude-code').includes('upload command returned by'));
 assert.ok(body('claude').includes('already imported'));
 assert.equal(text.includes('content_base64'),false);
});

test('downloadable SVG uses a valued XML attribute and matches the inline workflow',async()=>{
 const svg=await readFile(new URL('assets/sdk-workflow.svg',dist),'utf8');
 const html=await readFile(new URL('build/index.html',dist),'utf8');
 assert.ok(svg.includes('data-sdk-flow=""'));
 assert.ok(svg.includes('xmlns="http://www.w3.org/2000/svg"'));
 assert.ok(html.includes(svg.trim()));
});

test('standalone SDK SVG defines light and dark materials',async()=>{
 const svg=await readFile(new URL('assets/sdk-workflow.svg',dist),'utf8');
 assert.ok(svg.includes(':root.sdk-flow-svg'));
 assert.ok(svg.includes('--sdk-bg:#efefef'));
 assert.ok(svg.includes('--sdk-bg:#181818'));
 assert.ok(svg.includes('fill:var(--sdk-bg)'));
 assert.ok(svg.indexOf('class="co-region"')<svg.indexOf('data-path="conversation"'));
});
