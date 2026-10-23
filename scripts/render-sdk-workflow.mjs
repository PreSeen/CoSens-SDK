import {readFile,writeFile} from 'node:fs/promises';
const page=new URL('../dist/build/index.html',import.meta.url);
const svg=await readFile(new URL('../dist/assets/sdk-workflow.svg',import.meta.url),'utf8');
const html=await readFile(page,'utf8');
const marker=/<!-- sdk-workflow:start -->[\s\S]*?<!-- sdk-workflow:end -->/;
if(!marker.test(html))throw new Error('Missing SDK workflow markers');
await writeFile(page,html.replace(marker,()=>`<!-- sdk-workflow:start --><div class="sdk-diagram-scroll">${svg}</div><!-- sdk-workflow:end -->`));
console.log('Rendered SDK workflow into Build.');
