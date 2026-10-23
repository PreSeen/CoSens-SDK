import {test} from 'node:test';
import assert from 'node:assert/strict';
import {copyExample} from '../dist/app.mjs';
test('a browser without clipboard access receives an honest visible state',async()=>{
  assert.equal(await copyExample('client = cosens(client)',undefined),'Copy unavailable — select code');
});
test('a denied clipboard request is not reported as copied',async()=>{
  assert.equal(await copyExample('demo',{writeText:()=>Promise.reject(new Error('Permission denied'))}),'Copy blocked — select code');
});
test('a successful copy writes the exact selected example',async()=>{
  let copied;const text='client = openai()\nclient = cosens(client)';
  assert.equal(await copyExample(text,{writeText:async value=>{copied=value;}}),'Copied ✓');
  assert.equal(copied,text);
});
