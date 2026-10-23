import {readFile,writeFile} from 'node:fs/promises';
import {readPreference,resolveTheme} from '../dist/theme.mjs';

const dist=new URL('../dist/',import.meta.url);
const stylesheet=await readFile(new URL('styles.css',dist),'utf8');
const palette=stylesheet.split('\n').filter(line=>line.startsWith(':root{--bg:')||line.startsWith(':root[data-theme=dark]{--bg:'));
if(palette.length!==2)throw new Error('Expected the canonical light and dark palette rules');
// Generate from the same functions and palette used after load, so they cannot drift.
const bootstrap=`(()=>{${readPreference.toString()}\n${resolveTheme.toString()}\ndocument.documentElement.dataset.theme=resolveTheme(readPreference(localStorage),new Date().getHours());})();`;
const paint=`${palette.join('\n')}\n:root{background:var(--bg);color:var(--ink)}`;
const marker=/<!-- theme-paint:start -->[\s\S]*?<!-- theme-paint:end -->/;
for(const page of ['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html']){
 const file=new URL(page,dist);
 const html=await readFile(file,'utf8');
 if(!marker.test(html))throw new Error(`Missing early theme marker in ${page}`);
 await writeFile(file,html.replace(marker,()=>`<!-- theme-paint:start --><style id="theme-paint">${paint}</style><script id="theme-bootstrap">${bootstrap}</script><!-- theme-paint:end -->`));
}
console.log('Embedded first-paint theme and palette before external resources.');
