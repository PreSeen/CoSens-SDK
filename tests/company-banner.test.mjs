import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile,access} from 'node:fs/promises';
const root=new URL('../',import.meta.url);
test('company banner follows hero, precedes Sensable AI, without visible heading, caption or motion text',async()=>{
 const html=await readFile(new URL('dist/index.html',root),'utf8');
 assert.ok(html.indexOf('id="company-banner"')>html.indexOf('id="brain-diagram"'));
 assert.ok(html.indexOf('id="company-banner"')<html.indexOf('id="experience"'));
 assert.equal(html.includes('Selected current and past customers of the PreSeen team.'),false);
 assert.equal(html.includes('Experience built with leading companies.'),false);
 assert.equal(html.includes('company-pause-text'),false);
 assert.ok(html.includes('aria-label="Pause logo animation"'));
 assert.ok(html.includes('id="company-motion"'));
 for(const name of ['walmart','mercedes-benz','byd','bosch','continental','zf','bytedance','ant-group','bigmodel','minimax','jd','oppo']){
  await access(new URL(`dist/assets/companies/${name}.png`,root));
  assert.ok(html.includes(`/assets/companies/${name}.png`));
 }
 assert.ok(html.includes('class="company-set" aria-hidden="true"'));
});
test('company motion supports pause, hover and reduced-motion scrolling',async()=>{
 const css=await readFile(new URL('dist/styles.css',root),'utf8');
 assert.ok(css.includes('#company-motion:checked~.company-viewport .company-track'));
 assert.ok(css.includes('.company-viewport:hover .company-track'));
 assert.match(css,/@media\s*\(prefers-reduced-motion:reduce\)\{\.company-track\{animation:none/);
});
