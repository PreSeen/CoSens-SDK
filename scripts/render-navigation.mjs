import {readdir,readFile,writeFile} from 'node:fs/promises';
const dist=new URL('../dist/',import.meta.url);
const pages=(await readdir(dist,{recursive:true})).filter(p=>p==='index.html'||p.endsWith('/index.html'));
const brand='<span class="brand-word"><span class="brand-co">Co</span><span class="brand-sens">Sens</span></span>';
const links=[['/','Home'],['/avatar/','Avatar'],['/build/','Build'],['/pricing/','Pricing'],['/docs/','Docs']];
for(const page of pages){
 const file=new URL(page,dist);let html=await readFile(file,'utf8');
 const current=page==='index.html'?'/':'/'+page.slice(0,-10);
 const nav=links.map(([href,label])=>`<a href="${href}"${href===current?' aria-current="page"':''}>${label}</a>`).join('');
 const header=`<header class="nav"><a href="/" class="brand" aria-label="CoSens home">${brand}</a><nav class="nav-links" aria-label="Main navigation">${nav}</nav><div class="nav-right"><label class="visually-hidden" for="theme">Color theme</label><select id="theme" class="theme-select" data-theme-control aria-label="Color theme"><option value="auto">◐ Auto</option><option value="light">○ Light</option><option value="dark">● Dark</option></select><a class="btn small nav-access" href="/request-access/">Request access ↗</a><a class="nav-login" href="https://cloud.preseen.ai/" target="_blank" rel="noopener noreferrer">Log in</a></div></header>`;
 html=html.replace(/<header class="nav">[\s\S]*?<\/header>/,()=>header);
 html=html.replace(/<div class="footer-links">[\s\S]*?<\/div>/,()=>`<div class="footer-links">${links.map(([href,label])=>`<a href="${href}">${label}</a>`).join('')}<a href="/build/#benchmarks">Benchmarks</a></div>`);
 if(!html.includes('class="footer-links"'))html=html.replace('<div class="footer-right">',`<div class="footer-links">${links.map(([href,label])=>`<a href="${href}">${label}</a>`).join('')}</div><div class="footer-right">`);
 if(!html.includes('href="/product-preview.css'))html=html.replace('</head>','<link rel="stylesheet" href="/product-preview.css"></head>');
 await writeFile(file,html);
}
console.log('Rendered approved Home / Avatar / Build / Pricing / Docs navigation.');
