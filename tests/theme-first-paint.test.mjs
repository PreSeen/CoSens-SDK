import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {runInNewContext} from 'node:vm';

for(const file of ['index.html','build/index.html','docs/index.html','benchmarks/index.html','pricing/index.html','avatar/index.html','request-access/index.html']){
 test(`${file} chooses its first-paint theme before external resources`,async()=>{
  const html=await readFile(new URL('../dist/'+file,import.meta.url),'utf8');
  const script=html.match(/<script id="theme-bootstrap">([\s\S]*?)<\/script>/);
  assert.ok(script,'Theme must be available in HTML, without waiting for a module fetch');
  assert.ok(html.indexOf('id="theme-bootstrap"')<html.indexOf('rel="stylesheet"'));
  assert.ok(html.indexOf('id="theme-paint"')<html.indexOf('rel="stylesheet"'));
  const css=html.match(/<style id="theme-paint">([\s\S]*?)<\/style>/)[1];
  assert.ok(css.includes('--bg:#111111'));
  assert.ok(css.includes('background:var(--bg)'));
  for(const [saved,hour,expected]of [['auto',2,'dark'],['auto',12,'light'],['dark',12,'dark'],['light',2,'light'],[null,2,'dark'],['unknown',12,'light']]){
   const root={dataset:{}};
   runInNewContext(script[1],{document:{documentElement:root},localStorage:{getItem:key=>{assert.equal(key,'cosens-theme');return saved;}},Date:class{getHours(){return hour;}}});
   assert.equal(root.dataset.theme,expected,`${saved}, ${hour}`);
  }
 });
}
