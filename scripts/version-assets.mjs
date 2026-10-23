import {readFile,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
const dist=new URL('../dist/',import.meta.url);
const hash=async file=>createHash('sha256').update(await readFile(new URL(file,dist))).digest('hex').slice(0,12);
// Version shared imports before hashing the Docs and conversation modules.
for(const [name,prefix]of [['docs/docs.mjs','../'],['connect-story.mjs','./'],['capabilities.mjs','./']]){
 const file=new URL(name,dist);
 const source=await readFile(file,'utf8');
 const start=source.indexOf("from '")+6;
 const end=source.indexOf("'",start);
 await writeFile(file,source.slice(0,start)+`${prefix}app.mjs?v=${await hash('app.mjs')}`+source.slice(end));
}
for(const page of ['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html']){
  const file=new URL(page,dist);
  let html=await readFile(file,'utf8');
  const references=[...html.matchAll(/(?:href|src)="(\/[^"?]+\.(?:css|mjs|json))(?:\?v=[^"]+)?"/g)];
  for(const [attribute,path] of references){
    const versioned=attribute.replace(/(\.css|\.mjs|\.json)(?:\?v=[^"]+)?"$/,`$1?v=${await hash(path.slice(1))}"`);
    html=html.replace(attribute,versioned);
  }
  await writeFile(file,html);
}
console.log('Content-versioned stylesheet, scripts, and shared module imports.');
