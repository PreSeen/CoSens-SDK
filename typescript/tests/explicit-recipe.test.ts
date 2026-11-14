import assert from 'node:assert/strict';
import { test } from 'node:test';
import { randomUUID } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Agent } from '@openai/agents';
import { CoSensClient, CoSensAgentRunner, cosens } from '../src/index.ts';

for(const entry of ['transparent','agent','atomic'])test(`explicit generation: ${entry}`,async()=>{
 const paths:string[]=[];let count=0;let generations=0;
 const owner='ts-http-'+randomUUID();const local=process.env.COSENS_TEST_ALGORITHM_HTTP_URL;
 const fetcher:typeof fetch=async(input,init)=>{
  const url=new URL(String(input));const path=url.pathname.replace(/^\/api/,'');paths.push(path);
  assert.ok(!path.includes('/runs')&&!path.includes('/attributions')&&!path.includes('/improvements'));
  const body=init?.body?JSON.parse(String(init.body)):undefined;
  if(path.endsWith('/executions'))assert.equal(body.model,'model');
  if(local){const headers=new Headers(init?.headers);headers.set('X-User-Id',owner);return fetch(local+path+url.search,{...init,headers});}
  let value:any;
  if(path==='/recipe-candidates')value={items:[],has_more:false};
  else if(path==='/v1/memory/recall')value={items:[]};
  else if(path==='/artifacts')value={artifact_id:'artifact_'+(++count),content_hash:'sha256:test'};
  else if(path==='/tasks')value={task_id:'task_1',status:'ready'};
  else if(path.endsWith('/executions')||path.endsWith('/revisions'))value={execution_id:'execution_1',request_sha256:'hash',status:'awaiting_execution',request:{context:{objective:'Produce output'}}};
  else if(path.endsWith('/result'))value={revision_id:'revision_1',status:'completed'};
  else if(path.endsWith('/feedback')){assert.equal(body.source,'human');value={feedback_id:'feedback_1'};}
  else if(path.endsWith('/recipe-candidates')){assert.equal(body.include_memory,undefined);assert.equal(body.recall_memory,undefined);value={recipe_candidate_id:'candidate_'+(++generations),recipe_id:'recipe_1',version:generations,status:'queued'};}
  else if(path.startsWith('/recipe-candidates/candidate_'))value={status:'candidate',version:generations,recipe_id:'recipe_1',recipe_candidate_id:'candidate_'+generations,attribution:{possible_causes:[{reason:'format'}]}};
  else throw new Error(path);
  return new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
 };
 const statePath=join(mkdtempSync(join(tmpdir(),'explicit-ts-')),'state.sqlite');
 const client=new CoSensClient({apiKey:'test-key',fetch:fetcher,statePath});
 let wrapper:any;let taskId:string;let revisionId:string;
 if(entry==='transparent'){
  const native={responses:{create:async()=>({id:'response_1',status:'completed',output_text:'final output',output:[{type:'message'}]})},chat:{completions:{create:async()=>({choices:[{message:{content:JSON.stringify({activate:true,reason:'task'})}}]})}}};
  wrapper=cosens(native,{apiKey:'test-key',mode:'auto',fetch:fetcher,statePath});
  const response=await wrapper.responses.create({model:'model',input:'Produce output'});assert.equal(response.output_text,'final output');
  const turn=wrapper.cosens.getTurn(wrapper.cosens.lastTurnId);assert.equal(turn.state,'completed');taskId=turn.taskId;revisionId=turn.revisionId;
 }else if(entry==='agent'){
  const agent=new Agent({name:'reporter',instructions:'Produce output',model:'model'});
  wrapper=new CoSensAgentRunner(agent,{apiKey:'test-key',mode:'disable',fetch:fetcher,statePath,runner:{run:async()=>({finalOutput:'final output',newItems:[],interruptions:[]})}});
  const result=await wrapper.run(agent,'Produce output');assert.equal(result.finalOutput,'final output');
  const turn=wrapper.cosens.getTurn(wrapper.cosens.lastTurnId);assert.equal(turn.state,'completed');taskId=turn.taskId;revisionId=turn.revisionId;
 }else{
  const task=await client.atoms.createTask({source:'new_task',objective:'Produce output',selfContained:true,key:'task'});taskId=task.task_id;
  const execution=await client.atoms.startExecution(taskId,{key:'execution',model:'model'});
  const artifact=await client.atoms.artifact({name:'output.txt',content:'final output'});
  const result=await client.atoms.submitExecution(execution.execution_id,{requestSha256:execution.request_sha256,outcome:'completed',artifacts:[{artifact_id:artifact.artifact_id,role:'submitted_output'}]});revisionId=result.revision_id;
 }
 assert.ok(taskId!&&revisionId!);
 assert.ok(!paths.some(p=>p.endsWith('/recipe-candidates')&&p!=='/recipe-candidates'));
 const first=entry==='transparent'?await wrapper.cosens.generateRecipe():entry==='agent'?await wrapper.cosens.generateRecipe():await client.atoms.startRecipeCandidate(taskId!,{revisionId:revisionId!,key:'confirmed-v1'});
 const v1=await client.atoms.waitWorkflowJob('recipe_candidate',first.recipe_candidate_id,{timeout:10});assert.equal(v1.status,'candidate');assert.equal(v1.version,1);
 await client.atoms.recordFeedback(revisionId!,{feedback:'Use JSON without code fences',source:'human',key:'feedback'});
 const correction=await client.atoms.startRevision(revisionId!,{key:'correction'});
 const artifact=await client.atoms.artifact({name:'corrected.json',content:'{"answer":42}'});
 const final=await client.atoms.submitExecution(correction.execution_id,{requestSha256:correction.request_sha256,outcome:'completed',artifacts:[{artifact_id:artifact.artifact_id,role:'submitted_output'}]});
 const second=await client.atoms.startRecipeCandidate(taskId!,{revisionId:final.revision_id,key:'confirmed-v2'});
 const v2=await client.atoms.waitWorkflowJob('recipe_candidate',second.recipe_candidate_id,{timeout:10});assert.equal(v2.status,'candidate');assert.equal(v2.version,2);assert.equal(v2.recipe_id,v1.recipe_id);assert.ok(v2.attribution.possible_causes.length);
 await wrapper?.close();await client.close();
});
