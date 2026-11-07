import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CoSensClient, cosens } from '../src/index.ts';

function fixture() {
  const requests: Array<{path:string;body:any}> = [];
  const fetcher: typeof fetch = async (input, init) => {
    const path = new URL(String(input)).pathname.replace(/^\/api/, '');
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({path,body});
    let value:any;
    if(path==='/v1/recipe-candidates') value={items:[],has_more:false};
    else if(path==='/v1/memory/recall') value={items:[]};
    else if(path==='/v1/artifacts') value={artifact_id:'artifact',content_hash:'sha256:test'};
    else if(path==='/v1/tasks') value={task_id:'task',status:'ready'};
    else if(path.endsWith('/executions')) value={execution_id:'execution',request_sha256:'hash',request:{context:{objective:'Produce output'}},status:'awaiting_execution'};
    else if(path.endsWith('/result')) value={revision_id:'revision',status:'completed'};
    else value={recipe_candidate_id:'candidate',status:'queued'};
    return new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
  };
  return {requests,fetcher,statePath:join(mkdtempSync(join(tmpdir(),'recipe-budget-')),'state.sqlite')};
}

test('atomic start and resume preserve budgets and the legacy positional model',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher,statePath:f.statePath});
  try {
    await c.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'key',maxCompletionTokens:200_000});
    await c.atoms.resumeRecipeCandidate('candidate',{model:'model',maxCompletionTokens:300_000});
    await c.atoms.resumeRecipeCandidate('candidate','legacy-model');
    await c.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'default'});
    assert.equal(f.requests[0].body.data.max_completion_tokens,200_000);
    assert.deepEqual(f.requests[1].body.data,{model:'model',max_completion_tokens:300_000});
    assert.deepEqual(f.requests[2].body.data,{model:'legacy-model'});
    assert.equal(f.requests[3].body.data.max_completion_tokens,undefined);
  } finally {await c.close();}
});

test('invalid budgets never reach HTTP',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher,statePath:f.statePath});
  try {
    for(const maxCompletionTokens of [0,-1,1.2,NaN,Infinity,1_000_001,true,'200000']) {
      assert.throws(()=>c.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'key',maxCompletionTokens:maxCompletionTokens as number}),/maxCompletionTokens/);
      assert.throws(()=>c.atoms.resumeRecipeCandidate('candidate',{maxCompletionTokens:maxCompletionTokens as number}),/maxCompletionTokens/);
    }
    assert.equal(f.requests.length,0);
  } finally {await c.close();}
});

test('auto generateRecipe forwards its budget separately from business output tokens',async()=>{
  const f=fixture();
  const business:any[]=[];
  const native={responses:{create:async(params:any)=>{
    business.push(params);return {output_text:'final output',output:[{type:'message'}]};
  }},chat:{completions:{create:async()=>({choices:[{message:{content:JSON.stringify({activate:true,reason:'task'})}}]})}}};
  const wrapped=cosens(native,{apiKey:'key',mode:'auto',fetch:f.fetcher,statePath:f.statePath});
  try {
    await wrapped.responses.create({model:'model',input:'Produce output',max_output_tokens:4096});
    await wrapped.cosens.generateRecipe({maxCompletionTokens:200_000});
    const generated=f.requests.find(r=>r.path.endsWith('/recipe-candidates')&&r.body);
    assert.equal(generated?.body.data.max_completion_tokens,200_000);
    assert.equal(business[0].max_output_tokens,4096);
  } finally {await wrapped.close();}
});
