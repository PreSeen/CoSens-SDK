import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile,readdir} from 'node:fs/promises';
import {stages,dinnerOptions,checkDinnerOption} from '../dist/app.mjs';
const dist=new URL('../dist/',import.meta.url);
test('published text does not reintroduce the rejected placeholder wording',async()=>{
 async function walk(dir){for(const entry of await readdir(dir,{withFileTypes:true})){const url=new URL(entry.name+(entry.isDirectory()?'/':''),dir);if(entry.isDirectory())await walk(url);else if(entry.name.endsWith('.html')||entry.name.endsWith('.mjs')){const text=await readFile(url,'utf8');assert.equal(/\billustrative\b/i.test(text),false,url.pathname);}}}
 await walk(dist);
});

test('public pages use the two-color CoSens wordmark and preserve executable identifiers',async()=>{
 for(const file of ['index.html','build/index.html','docs/index.html','benchmarks/index.html']){
  const html=await readFile(new URL(file,dist),'utf8');
  assert.ok(html.includes('<span class="brand-co">Co</span><span class="brand-sens">Sens</span>'),file);
  assert.equal(html.includes('Cosens'),false,file);
  assert.equal(html.includes('<svg class="mark"'),false,file);
 }
 const app=await readFile(new URL('app.mjs',dist),'utf8');
 assert.ok(app.includes('client = cosens(client)'));
 const docs=await readFile(new URL('docs/docs.mjs',dist),'utf8');
 assert.ok(docs.includes('decorateBrand(content)'));
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.ok(css.includes('--brand-co:#383d45;--brand-sens:#246bfe'));
 assert.ok(css.includes('--brand-co:#d1d5db;--brand-sens:#73a4ff'));
});

test('results and supporting tools stay distinct across the site',async()=>{
 const home=await readFile(new URL('build/index.html',dist),'utf8');
 assert.ok(home.includes('REASON + REMEMBER + IMPROVE'));
 assert.ok(home.includes('Improve with Learn'));
 assert.equal(home.includes('REASON + REMEMBER + LEARN'),false);
 const docs=await readFile(new URL('docs/docs.mjs',dist),'utf8');
 for(const pair of ["['LLM','Reason —","['Memory','Remember —","['Learn','Improve —"])assert.ok(docs.includes(pair),pair);
 assert.ok(docs.includes("learn:{nav:'Learn'"));
 const shell=await readFile(new URL('docs/index.html',dist),'utf8');
 assert.ok(shell.includes('href="#learn"'));
 assert.equal(shell.includes('href="#learning"'),false);
});
test('optional concept model preserves overlapping planes and separate flow arrows',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 const svg=html.slice(html.indexOf('<svg viewBox="0 0 720 480"'),html.indexOf('</svg>',html.indexOf('<svg viewBox="0 0 720 480"'))+6);
 assert.equal([...svg.matchAll(/class="experience-plane"/g)].length,3);
 assert.ok(svg.includes('class="flow-input" d="M155 136v76M155 236v76"'));
 assert.ok(svg.includes('class="flow-output" d="M325 325v-76M325 225v-76"'));
 assert.equal(svg.includes('orbit-track'),false);
 assert.ok(svg.includes('learn-pyramid'),'Learn uses a two-tier triangular solid');
 assert.equal(svg.includes('cx="335" cy="320" r="5"'),false);
 const ranges=[...svg.matchAll(/class="stack-plane" d="M(\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+) (\d+)Z"/g)].map(m=>[Number(m[2]),Number(m[6])]).sort((a,b)=>a[0]-b[0]);
 assert.equal(ranges.length,3);
 assert.ok(ranges[0][1]>ranges[1][0]&&ranges[1][1]>ranges[2][0],'adjacent layers must visibly overlap');
});

test('hero diagram blends into the page without card chrome',async()=>{
 const css=await readFile(new URL('styles.css',dist),'utf8');
 const rule=css.match(/\.brain-diagram\{([^}]+)\}/)[1];
 assert.equal(rule.includes('border:'),false);
 assert.equal(rule.includes('box-shadow:'),false);
 assert.ok(rule.includes('background:transparent'));
});

test('dinner timing check transfers to new options without losing remembered preferences',()=>{
 assert.equal(checkDinnerOption(dinnerOptions[0]).passes,false);
 assert.equal(checkDinnerOption(dinnerOptions[0]).marginMinutes,-5);
 assert.equal(checkDinnerOption(dinnerOptions[1]).passes,true);
 assert.equal(checkDinnerOption(dinnerOptions[1]).marginMinutes,40);
 assert.equal(checkDinnerOption({...dinnerOptions[1],vegetarian:false}).passes,false);
 assert.equal(checkDinnerOption({...dinnerOptions[1],quiet:false}).passes,false);
 assert.equal(checkDinnerOption(dinnerOptions[1],21*60).passes,false);
});
test('layer buttons and inline case replace the separate evidence workflow',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 for(let index=0;index<3;index++)assert.ok(html.includes(`data-layer="${index}" role="button" tabindex="0"`));
 assert.ok(html.includes('id="layer-detail"'));
 assert.equal(html.includes('/evidence/'),false);
 assert.equal(html.includes('<title id="brain-title">'),false,'no native SVG hover tooltip');
 assert.ok((await readFile(new URL('index.html',dist),'utf8')).includes('SAMPLE SCENARIO'));
 const rules=await readFile(new URL('_redirects',dist),'utf8');assert.ok(rules.includes('/evidence/site-revision /#how-it-works 302'));
 assert.ok(stages[2].input.includes('A better approach'));
 assert.ok(stages[3].body.includes('Both options are quiet and vegetarian'));
});

test('layer explanations sit beside the stack and have hover and focus triggers',async()=>{
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.ok(css.includes('.layer-detail{position:absolute;left:78%;width:21%'));
 const app=await readFile(new URL('app.mjs',dist),'utf8');
 assert.ok(app.includes("layer.addEventListener('pointerenter'"));
 assert.ok(app.includes("layer.addEventListener('focus'"));
 const home=await readFile(new URL('index.html',dist),'utf8');
 assert.ok(home.includes('id="layer-leader"'));
});

test('each layer has a distinct dimensional object',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 for(const symbol of ['reason-cube','memory-cluster','learn-pyramid'])assert.ok(html.includes(symbol));
});

test('all layer objects share one neutral material palette',async()=>{
 const css=await readFile(new URL('styles.css',dist),'utf8');
 for(const token of ['--sphere-light:var(--object-light)','--sphere-mid:var(--object-mid)','--cube-top:var(--object-light)','--cube-right:var(--object-dark)'])assert.ok(css.includes(token));
 const html=await readFile(new URL('index.html',dist),'utf8');
 assert.ok(css.includes('.tetra-left{fill:var(--object-mid)}'));
 assert.equal(html.includes('hsl(220 76%'),false,'the strip must not use a separate saturated blue material');
});

test('Memory graph keeps a regular node arrangement and Learn retains one reused unit',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 const cluster=html.slice(html.indexOf('<g class="memory-cluster'),html.indexOf('</g>',html.indexOf('<g class="memory-cluster')));
 const spheres=[...cluster.matchAll(/<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"/g)];
 assert.equal(spheres.length,6);assert.ok(spheres.every(match=>match[3]==='7'));
 for(const [row,count]of [[0,3],[1,2],[2,1]])assert.equal([...cluster.matchAll(new RegExp(`data-row="${row}"`,'g'))].length,count);
 assert.equal([...html.matchAll(/class="tetra-unit reused"/g)].length,1);
 assert.ok(html.includes('data-tier="1"'));assert.ok(html.includes('data-tier="0"'));
});

test('layer contents use the same projection and translucent platforms',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 assert.equal([...html.matchAll(/data-projection="isometric"/g)].length,2);
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.ok(css.includes('.brain-diagram .stack-plane{fill-opacity:.7}'));
 assert.equal(css.includes('fill-opacity:.96'),false);
});

test('mobile Build hero preserves two complete phrases with fluid type',async()=>{
 const html=await readFile(new URL('build/index.html',dist),'utf8');
 assert.ok(html.includes('<span class="hero-line">Build your second brain</span>'));
 assert.ok(html.includes('<span class="hero-line">with one line of code.</span>'));
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.ok(css.includes('.hero-line{display:block;white-space:nowrap}'));
 assert.ok(css.includes('font-size:clamp(24px,10.9vw,57px)'));
});

test('mobile benchmark labels wrap inside a bounded first column',async()=>{
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.ok(css.includes('.benchmark-table th:first-child{white-space:normal;min-width:140px;max-width:160px;width:160px}'));
});

test('mobile keeps the six diagram labels visible',async()=>{
 const css=await readFile(new URL('styles.css',dist),'utf8');
 assert.equal(css.includes('.stack-label{display:none}'),false);
 assert.ok(css.includes('.brain-diagram .stack-label{display:block}'));
});


test('Memory graph has visible connections and every node belongs to one connected graph',async()=>{
 const html=await readFile(new URL('index.html',dist),'utf8');
 const cluster=html.slice(html.indexOf('<g class="memory-cluster'),html.indexOf('</g>',html.indexOf('<g class="memory-cluster')));
 const nodes=new Set([...cluster.matchAll(/id="(memory-node-\d+)"/g)].map(match=>match[1]));
 const edges=[...cluster.matchAll(/data-from="([^"]+)" data-to="([^"]+)"/g)].map(match=>[match[1],match[2]]);
 assert.equal(nodes.size,6);assert.equal(edges.length,9);
 const seen=new Set([nodes.values().next().value]);
 for(const [a,b]of edges){assert.ok(nodes.has(a));assert.ok(nodes.has(b));assert.notEqual(a,b);}
 for(let pass=0;pass<nodes.size;pass++)for(const [a,b]of edges){if(seen.has(a))seen.add(b);if(seen.has(b))seen.add(a);}
 assert.equal(seen.size,nodes.size);
});
