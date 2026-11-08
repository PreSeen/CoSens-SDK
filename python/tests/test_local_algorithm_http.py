"""Opt-in integration with the local algorithm server documented in the repository README."""
import os
import json
from uuid import uuid4
from types import SimpleNamespace

import httpx
import pytest
from agents import Agent
from openai import OpenAI

from cosens import CoSensClient, cosens
from test_explicit_recipe_protocol import native_response

URL=os.environ.get('COSENS_TEST_ALGORITHM_HTTP_URL')
pytestmark=pytest.mark.skipif(not URL,reason='Requires the local PostgreSQL-backed algorithm HTTP fixture')


@pytest.mark.asyncio
@pytest.mark.parametrize('entry',['transparent','agent','atomic'])
async def test_three_entries_against_local_algorithm(tmp_path,entry):
    # Emulate only the gateway identity header; all algorithm requests use real loopback HTTP.
    owner='sdk-http-'+uuid4().hex
    observed=[]
    remote=httpx.Client(base_url=URL,timeout=15)
    def forward(request):
        path=request.url.path.removeprefix('/api')
        observed.append(path)
        response=remote.request(request.method,path,params=request.url.params,content=request.content,headers={'X-User-Id':owner,'Authorization':request.headers.get('Authorization',''),'X-Auth-Type':'api_key','Content-Type':'application/json',**({'Idempotency-Key':request.headers['Idempotency-Key']} if 'Idempotency-Key' in request.headers else {})})
        return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
    transport=httpx.MockTransport(forward)
    sdk=CoSensClient(api_key='local-test-key',state_path=tmp_path/'atoms.sqlite',_http_transport=transport)
    native=None;wrapped=None
    try:
        if entry=='transparent':
            native=OpenAI(base_url='https://llm.cosens.ai/v1',api_key='local-test-key',http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
            wrapped=cosens(native,mode='auto',api_key='local-test-key',state_path=tmp_path/'wrapper.sqlite',_http_transport=transport)
            wrapped.responses.create(model='model',input='Produce a structured report')
            turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        elif entry=='agent':
            agent=Agent(name='reporter',instructions='Produce a report',model='model')
            class Runner:
                @staticmethod
                async def run(*args,**kwargs):return SimpleNamespace(final_output='report',new_items=[],interruptions=[])
            wrapped=cosens(agent,mode='disable',api_key='local-test-key',state_path=tmp_path/'wrapper.sqlite',_http_transport=transport,_runner=Runner)
            await wrapped.run(agent,'Produce a structured report')
            turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        else:
            task=await sdk.atoms.create_task(source='new_task',objective='Produce a structured report',self_contained=True,key='task')
            execution=await sdk.atoms.start_execution(task['task_id'],key='execution',model='model')
            artifact=await sdk.atoms.artifact(name='report.txt',content='report')
            revision=await sdk.atoms.submit_execution(execution['execution_id'],request_sha256=execution['request_sha256'],outcome='completed',artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
            turn={'task_id':task['task_id'],'revision_id':revision['revision_id']}
        assert turn.get('revision_id'),turn
        execution_id = turn.get('execution_id') or execution['execution_id']
        captured = await sdk.atoms.get_execution(execution_id)
        assert captured['request']['model'] == 'model'
        assert (await sdk.atoms.list_recipe_candidates())['items']==[]
        if entry=='transparent':
            request=wrapped.cosens.generate_recipe()
        elif entry=='agent':request=await wrapped.cosens.generate_recipe()
        else:request=await sdk.atoms.start_recipe_candidate(turn['task_id'],revision_id=turn['revision_id'],key='confirmed-v1')
        v1=await sdk.atoms.wait_workflow_job('recipe_candidate',request['recipe_candidate_id'],timeout=10)
        assert v1['status']=='candidate' and v1['version']==1
        assert v1['memory_context']=='captured'
        await sdk.atoms.record_feedback(turn['revision_id'],feedback='Use bare JSON without code fences',source='human',key='feedback')
        correction=await sdk.atoms.start_revision(turn['revision_id'],key='correction')
        artifact=await sdk.atoms.artifact(name='corrected.json',content='{"answer":42}')
        result=await sdk.atoms.submit_execution(correction['execution_id'],request_sha256=correction['request_sha256'],outcome='completed',artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
        assert len((await sdk.atoms.list_recipe_candidates())['items'])==1
        second=await sdk.atoms.start_recipe_candidate(turn['task_id'],revision_id=result['revision_id'],key='confirmed-v2')
        v2=await sdk.atoms.wait_workflow_job('recipe_candidate',second['recipe_candidate_id'],timeout=10)
        assert v2['status']=='candidate' and v2['version']==2
        assert v2['recipe_id']==v1['recipe_id'] and v2['parent_candidate_id']==v1['recipe_candidate_id']
        assert v2['attribution']['possible_causes']
        assert all('/runs' not in path and '/attributions' not in path for path in observed)
    finally:
        if entry=='agent' and wrapped:await wrapped.aclose()
        elif wrapped:wrapped.close()
        if native:native.close()
        await sdk.aclose();remote.close()
