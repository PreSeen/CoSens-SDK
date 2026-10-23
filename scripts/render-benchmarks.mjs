import {readFile,writeFile} from 'node:fs/promises';
const dist=new URL('../dist/',import.meta.url);
const report=JSON.parse(await readFile(new URL('benchmarks/results.json',dist),'utf8'));
const escape=text=>String(text).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const brand=text=>escape(text).replaceAll('CoSens','<span class="brand-word"><span class="brand-co">Co</span><span class="brand-sens">Sens</span></span>');
const links=items=>`<ul class="resource-links">${items.map(item=>`<li><a href="${escape(item.url)}">${escape(item.title)} ↗</a><span class="resource-url">${escape(item.url)}</span></li>`).join('')}</ul>`;
// The homepage is a curated view of the same source scores as the full report.
function table(systems,benchmarks,compact){
 return `<p class="benchmark-annotation">${systems.length} methods${compact?'':' · scroll horizontally to compare →'}</p><div class="benchmark-block"><div class="table-scroll" role="region" aria-label="Selected benchmark scores" tabindex="0"><table class="benchmark-table"><caption>Reported benchmark scores — higher is better</caption><thead><tr><th scope="col">Benchmark</th>${systems.map(system=>`<th scope="col" class="${system.id==='cosens'?'ours-column':''}">${escape(system.name).replaceAll('CoSens','<span class="brand-word"><span class="brand-co">Co</span><span class="brand-sens">Sens</span></span>')}</th>`).join('')}</tr></thead><tbody>${benchmarks.map(benchmark=>`<tr><th scope="row">${escape(benchmark.name)}<span class="benchmark-metric"><a href="${compact?'/benchmarks/':''}#benchmark-${escape(benchmark.id)}">Method &amp; sources ${compact?'↗':'↓'}</a></span><span class="benchmark-metric">${escape(benchmark.metric)} · ${escape(benchmark.scale)}</span><span class="benchmark-metric">${escape(benchmark.source)}</span></th>${systems.map(system=>`<td class="${system.id==='cosens'?'ours-column':''}">${benchmark.scores[system.id]===null?'—':benchmark.scores[system.id].toFixed(benchmark.precision)}</td>`).join('')}</tr>`).join('')}</tbody></table></div></div>`;
}
const homeSystems=report.homepage.systemIds.map(id=>report.systems.find(system=>system.id===id));
const homeBenchmarks=report.homepage.benchmarkNames.map(name=>report.benchmarks.find(benchmark=>benchmark.name===name));
for(const [file,marker,systems,benchmarks,compact]of [
 ['build/index.html','benchmark-tables',homeSystems,homeBenchmarks,true],
 ['benchmarks/index.html','benchmark-details',report.systems,report.benchmarks,false]
]){
 const path=new URL(file,dist);const text=await readFile(path,'utf8');const start=`<!-- ${marker}:start -->`;const end=`<!-- ${marker}:end -->`;
 const a=text.indexOf(start);const b=text.indexOf(end);
 if(a<0||b<a)throw new Error(`Missing benchmark output markers: ${file}`);
 await writeFile(path,text.slice(0,a+start.length)+table(systems,benchmarks,compact)+text.slice(b));
}
console.log('Rendered curated homepage comparison and full benchmark report.');

const originLabels={'company-run':'Company run','paper-reference':'Paper reference','local-baseline':'Local baseline'};
const headingLabels={purpose:'What it tests',dataset:'Dataset and selected task',procedure:'Evaluation procedure',scoring:'Metric and calculation',scope:'Scope of the reported run'};
const methodologies=report.benchmarks.map(b=>{
 const source=report.sources[b.sourceId];
 const origins=Object.entries(b.scoreOrigins).map(([id,origin])=>`${brand(report.systems.find(s=>s.id===id).name)}: ${originLabels[origin]}`).join(' · ');
 return `<details class="method-detail" id="benchmark-${escape(b.id)}"><summary><span>${escape(b.name)}</span><span class="detail-meta">${escape(b.metric)}</span></summary><div class="method-body">${links(b.links)}<dl>${Object.entries(b.methodology).map(([key,value])=>`<div><dt>${headingLabels[key]}</dt><dd>${brand(value)}</dd></div>`).join('')}<div><dt>Score provenance</dt><dd>${origins}</dd></div><div><dt>Reported score source</dt><dd><a href="${escape(source.url)}">${escape(source.title)} ↗</a><span class="resource-url">${escape(source.access)}</span></dd></div></dl></div></details>`;
}).join('');
const systems=report.systems.map(system=>`<article class="system-detail" id="system-${escape(system.id)}"><h3>${brand(system.name)}</h3><p>${brand(system.description)}</p>${links(system.links)}<p class="system-scope">${brand(system.evaluationNote)}</p></article>`).join('');
const content=`<nav class="report-nav" aria-label="Benchmark report sections"><a href="#method">Benchmark methods</a><a href="#systems">Compared memories</a><a href="#reporting">Sources &amp; run settings</a></nav><section id="method"><h2>Benchmark methods &amp; resources</h2><p>Open a benchmark for its original resources, procedure, selected metric and reported-run scope. These descriptions explain the public protocols; they are not a claim that every company run reproduced the current upstream configuration.</p><div class="method-list">${methodologies}</div></section><section id="systems"><h2>Compared memories &amp; reference methods</h2><p>Exact evaluated versions are not provided in the source reports. Project links identify the original systems; a current release may differ from the implementation behind a historical score.</p><div class="system-grid">${systems}</div></section><section id="reporting"><h2>Sources &amp; run settings</h2><p>Reviewed ${escape(report.reviewed)}. Higher is better within each row. Scores retain their original scales; unrelated tasks are not averaged into a composite total. A dash means no reported result.</p><div class="source-grid">${Object.values(report.sources).map(source=>`<article><h3>${escape(source.title)}</h3><p>${escape(source.description)}</p><a href="${escape(source.url)}">Open source report ↗</a><span class="resource-url">${escape(source.access)}</span></article>`).join('')}</div><p>The ${brand('CoSens')} column uses the memory implementation called Friday in the reports. These results do not benchmark the complete SDK or the Learn loop. ${escape(report.configuration)}</p><h3>Available reproduction information</h3><p>The fields below describe the company-run evidence available for this table. Missing settings remain explicit; upstream quickstarts are linked above and should not be read as the commands used for these runs.</p><dl class="run-metadata">${report.runMetadata.map(item=>`<div><dt>${escape(item.field)} <span class="detail-meta">${escape(item.status)}</span></dt><dd>${escape(item.detail)}</dd></div>`).join('')}</dl><p><a class="text-link" href="/benchmarks/results.json">Download scores, sources &amp; method notes (JSON) ↗</a></p></section>`;
const detailPath=new URL('benchmarks/index.html',dist);
const detailText=await readFile(detailPath,'utf8');
const methodStart=detailText.indexOf('<!-- benchmark-methods:start -->');
const methodEnd=detailText.indexOf('<!-- benchmark-methods:end -->');
if(methodStart<0||methodEnd<methodStart)throw new Error('Missing benchmark methodology output markers');
await writeFile(detailPath,detailText.slice(0,methodStart+'<!-- benchmark-methods:start -->'.length)+content+detailText.slice(methodEnd));
