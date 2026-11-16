import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensClient, cosens } from '../src/index.ts';
import { ServiceFake } from './service-fake.ts';

function fixture() {
  const requests: Array<{method:string;path:string;body:any}> = [];
  const fetcher: typeof fetch = async (input, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({method:init?.method??'GET',path:new URL(String(input)).pathname.replace(/^\/api/,''),body});
    return new Response(JSON.stringify({execution_id:'execution',status:'queued'}),{headers:{'Content-Type':'application/json'}});
  };
  return {requests,fetcher};
}

test('learn sends the web search fields in the POST /executions/{id}/learn body only when given',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher});
  try {
    await c.atoms.learn('execution');
    await c.atoms.learn('execution',{webSearch:false});
    await c.atoms.learn('execution',{webSearch:true,webSearchBlockedDomains:['github.com']});
    assert.deepEqual(f.requests.map(r=>[r.method,r.path]),new Array(3).fill(['POST','/executions/execution/learn']));
    assert.deepEqual(f.requests.map(r=>r.body),[{},{web_search:false},{web_search:true,web_search_blocked_domains:['github.com']}]);
  } finally {await c.close();}
});

test('resume carries no web search setting: the one frozen at learn time is used',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher});
  try {
    await c.atoms.resumeLearning('execution');
    assert.equal(f.requests[0].path,'/executions/execution/resume');
    assert.equal(f.requests[0].body,undefined);
  } finally {await c.close();}
});

test('invalid web search values never reach HTTP',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher});
  try {
    assert.throws(()=>c.atoms.learn('execution',{webSearch:'yes' as unknown as boolean}),/webSearch/);
    assert.throws(()=>c.atoms.learn('execution',{webSearchBlockedDomains:new Array(51).fill('a.com')}),/webSearchBlockedDomains/);
    assert.equal(f.requests.length,0);
  } finally {await c.close();}
});

test('auto generateRecipe passes the web search fields to learn',async()=>{
  const service=new ServiceFake();
  const native={responses:{create:async()=>({output_text:'final output',output:[{type:'message'}]})},
    chat:{completions:{create:async()=>({choices:[{message:{content:JSON.stringify({activate:true,reason:'task'})}}]})}}};
  const wrapped=cosens(native,{apiKey:'key',mode:'auto',fetch:service.fetch,statePath:join(mkdtempSync(join(tmpdir(),'recipe-web-')),'state.sqlite')});
  try {
    await wrapped.responses.create({model:'model',input:'Produce output'});
    await wrapped.cosens.generateRecipe({webSearch:true,webSearchBlockedDomains:['github.com']});
    assert.deepEqual(service.requests.filter(r=>r.path.endsWith('/learn')).map(r=>r.body),[{web_search:true,web_search_blocked_domains:['github.com']}]);
  } finally {await wrapped.close();}
});
