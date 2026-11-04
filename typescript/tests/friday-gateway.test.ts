import assert from 'node:assert/strict';
import test from 'node:test';
import { fridayGatewayClient, FRIDAY_US_BASE_URL } from '../src/friday-gateway.js';
import { CoSensClient } from '../src/index.js';

for(const region of ['cn','us'] as const)test(`One user API Key selects the ${region} gateway`,()=>{
 const previous=process.env.COSENS_API_KEY;process.env.COSENS_API_KEY='shared-user-key';
 try{const client=fridayGatewayClient(undefined,{region});assert.equal(client.apiKey,'shared-user-key');assert.equal(client.baseURL,region==='us'?FRIDAY_US_BASE_URL:'https://friday-llm-gateway.corpintra.rosettalab.top/v1');}
 finally{if(previous===undefined)delete process.env.COSENS_API_KEY;else process.env.COSENS_API_KEY=previous;}
 assert.throws(()=>fridayGatewayClient('bad token'));
});

test('Model selection sends no second credential or endpoint configuration',async()=>{
 const seen:any[]=[];
 const client=new CoSensClient({apiKey:'shared-user-key',fetch:async(input,init)=>{seen.push({headers:new Headers(init?.headers),body:JSON.parse(String(init?.body))});return new Response('{}',{headers:{'Content-Type':'application/json'}});}});
 await client.atoms.startRecipeCandidate('task',{revisionId:'rev',key:'confirmed',model:'public-model'});
 await client.atoms.resumeRecipeCandidate('candidate','public-model');
 assert.deepEqual(seen[0].body,{data:{revision_id:'rev',model:'public-model'}});
 assert.deepEqual(seen[1].body,{data:{model:'public-model'}});
 assert.ok(seen.every(r=>r.headers.get('Authorization')==='Bearer shared-user-key'&&!JSON.stringify(r.body).includes('shared-user-key')));
 await client.close();
});

test('auto gateway decisions use supported Chat JSON schema with the shared key',async()=>{
 const {structuredResponse}=await import('../src/structured.js');
 const paths:string[]=[];
 const native=fridayGatewayClient('shared-user-key',{region:'us'}).withOptions({fetch:async(input,init)=>{
  const request=input instanceof Request?input:new Request(input,init);
  const body=await request.json() as any;paths.push(new URL(request.url).pathname);
  assert.equal(request.headers.get('Authorization'),'Bearer shared-user-key');
  assert.equal(request.headers.get('X-Friday-Auth-Type'),'api_key');
  assert.equal(body.response_format.json_schema.name,'cosens_decision');assert.equal(body.text,undefined);assert.equal(body.store,undefined);
  return new Response(JSON.stringify({id:'decision',object:'chat.completion',created:0,model:'public-model',choices:[{index:0,message:{role:'assistant',content:'{"activate":true}'},finish_reason:'stop'}]}),{headers:{'Content-Type':'application/json'}});
 }});
 const response=await structuredResponse(native,{model:'public-model',input:'Write a report',text:{format:{type:'json_schema',name:'cosens_decision',strict:true,schema:{type:'object'}}},max_output_tokens:256,store:false});
 assert.equal(response.output_text,'{"activate":true}');assert.deepEqual(paths,['/v1/chat/completions']);
});
