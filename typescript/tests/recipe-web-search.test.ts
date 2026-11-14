import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createHash } from 'node:crypto';
import { CoSensClient } from '../src/index.ts';
import { recipeRequestKey } from '../src/http.ts';

function fixture() {
  const requests: Array<{path:string;body:any;key?:string}> = [];
  const fetcher: typeof fetch = async (input, init) => {
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({path:new URL(String(input)).pathname,body,key:new Headers(init?.headers).get('Idempotency-Key')??undefined});
    return new Response(JSON.stringify({recipe_candidate_id:'candidate',status:'queued'}),{headers:{'Content-Type':'application/json'}});
  };
  return {requests,fetcher};
}

test('web search fields reach the request only when given, and change the derived key',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher});
  try {
    await c.atoms.startRecipeCandidate('task',{revisionId:'revision'});
    await c.atoms.startRecipeCandidate('task',{revisionId:'revision',webSearch:false});
    await c.atoms.startRecipeCandidate('task',{revisionId:'revision',webSearchBlockedDomains:['github.com']});
    const [plain,off,blocked]=f.requests;
    assert.equal('web_search' in plain.body.data,false);assert.equal('web_search_blocked_domains' in plain.body.data,false);
    assert.equal(off.body.data.web_search,false);
    assert.deepEqual(blocked.body.data.web_search_blocked_domains,['github.com']);
    assert.equal(new Set([plain.key,off.key,blocked.key]).size,3);
  } finally {await c.close();}
});

test('the derived key matches the Python SDK for the same web search settings',()=>{
  assert.equal(recipeRequestKey('t','r'),'recipe-'+createHash('sha256').update('recipe:t:r:','utf8').digest('hex'));
  assert.equal(recipeRequestKey('t','r',undefined,false,['a.com']),'recipe-'+createHash('sha256').update('recipe:t:r::web=[false,["a.com"]]','utf8').digest('hex'));
});

test('invalid web search values never reach HTTP',async()=>{
  const f=fixture(); const c=new CoSensClient({apiKey:'key',fetch:f.fetcher});
  try {
    assert.throws(()=>c.atoms.startRecipeCandidate('task',{revisionId:'revision',webSearch:'yes' as unknown as boolean}),/webSearch/);
    assert.throws(()=>c.atoms.startRecipeCandidate('task',{revisionId:'revision',webSearchBlockedDomains:new Array(51).fill('a.com')}),/webSearchBlockedDomains/);
    assert.equal(f.requests.length,0);
  } finally {await c.close();}
});
