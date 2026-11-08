"""SDK contracts for explicit generation across all three public entry styles."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI, AsyncOpenAI
from agents import Agent

from cosens import CoSensClient, cosens


class Backend:
    def __init__(self):self.seen=[];self.count=0;self.generations=0
    def __call__(self,r):
        body=json.loads(r.content) if r.content else None
        path=r.url.path.removeprefix('/api');self.seen.append((r.method,path,body))
        assert '/runs' not in path and '/improvements' not in path and '/attributions' not in path
        if path=='/v1/recipe-candidates':return httpx.Response(200,json={'items':[],'has_more':False})
        if path=='/v1/memory/recall':return httpx.Response(200,json={'items':[]})
        if path=='/v1/artifacts':
            self.count+=1;return httpx.Response(200,json={'artifact_id':f'artifact_{self.count}','content_hash':'sha256:test'})
        if path=='/v1/tasks' and r.method=='POST':return httpx.Response(200,json={'task_id':'task_1','status':'ready'})
        if path.endswith('/executions') or path.endswith('/revisions') and r.method=='POST':
            return httpx.Response(200,json={'task_id':'task_1','execution_id':'execution_1','request_sha256':'request-hash','status':'awaiting_execution','request':{'context':{'objective':'task'}}})
        if path.endswith('/result'):return httpx.Response(200,json={'status':'completed','revision_id':'revision_1'})
        if path.endswith('/feedback'):
            assert body['source']=='human'
            return httpx.Response(200,json={'feedback_id':'feedback_1'})
        if path.endswith('/recipe-candidates'):
            assert 'recall_memory' not in body['data'] and 'include_memory' not in body['data']
            self.generations+=1
            return httpx.Response(200,json={'recipe_candidate_id':'candidate_1','recipe_id':'recipe_1','status':'queued','version':1,'parent_candidate_id':None})
        if path=='/v1/recipe-candidates/candidate_1':return httpx.Response(200,json={'status':'candidate','recipe_candidate_id':'candidate_1','recipe':{'steps':[]}})
        if path=='/v1/sessions/start':return httpx.Response(200,json={'session_id':'session_1','cursor':0})
        if path.endswith('/events'):return httpx.Response(200,json={'cursor':1,'accepted':1})
        raise AssertionError((r.method,path,body))


def native_response(r):
    data=json.loads(r.content)
    if r.url.path.endswith('/chat/completions'):
        text=json.dumps({'activate':True,'reason':'task'})
        return httpx.Response(200,json={'id':'decision','object':'chat.completion','created':0,'model':'model','choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]})
    fmt=data.get('text',{}).get('format',{})
    text=json.dumps({'activate':True,'reason':'task'}) if fmt.get('name')=='cosens_decision' else 'final artifact'
    return httpx.Response(200,json={'id':'response_1','object':'response','status':'completed','created_at':1,'model':'model',
        'output':[{'type':'message','id':'message_1','status':'completed','role':'assistant','content':[{'type':'output_text','text':text,'annotations':[]}]}]})


def test_transparent_sync_waits_for_explicit_generation(tmp_path):
    backend=Backend()
    native=OpenAI(api_key='model-key',http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    wrapped=cosens(native,mode='auto',api_key='service-key',state_path=tmp_path/'sync.sqlite',_http_transport=httpx.MockTransport(backend))
    response=wrapped.responses.create(model='model',input='Produce a report')
    assert response.output_text=='final artifact'
    assert backend.generations==0
    assert wrapped.cosens.wait_learning(timeout=0)['state']=='awaiting_confirmation'
    wrapped.cosens.feedback('Check the format', source='human')
    assert backend.generations==0
    receipt=wrapped.cosens.generate_recipe()
    assert receipt['recipe_candidate_id']=='candidate_1' and backend.generations==1
    wrapped.cosens.generate_recipe()
    assert backend.generations==1
    assert wrapped.cosens.wait_learning(timeout=1)['state']=='completed'
    wrapped.close();native.close()


@pytest.mark.asyncio
async def test_transparent_async_uses_same_explicit_contract(tmp_path):
    backend=Backend()
    native=AsyncOpenAI(api_key='model-key',http_client=httpx.AsyncClient(transport=httpx.MockTransport(native_response)))
    wrapped=cosens(native,mode='auto',api_key='service-key',state_path=tmp_path/'async.sqlite',_http_transport=httpx.MockTransport(backend))
    response=await wrapped.responses.create(model='model',input='Produce a report')
    assert response.output_text=='final artifact' and backend.generations==0
    await wrapped.cosens.generate_recipe()
    assert backend.generations==1
    await wrapped.close();await native.close()


@pytest.mark.asyncio
async def test_explicit_agent_run_records_task_without_generating(tmp_path):
    backend=Backend()
    final=SimpleNamespace(final_output='agent artifact',new_items=[],interruptions=[])
    class Runner:
        @staticmethod
        async def run(agent,user_input,**options):return final
    agent=Agent(name='reporter',instructions='Produce output')
    wrapped=cosens(agent,mode='disable',api_key='service-key',state_path=tmp_path/'agent.sqlite',_http_transport=httpx.MockTransport(backend),_runner=Runner)
    response=await wrapped.run(agent,'Produce a report')
    assert response is final
    turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
    assert turn['revision_id']=='revision_1' and backend.generations==0
    await wrapped.cosens.feedback('Check the format', source='human')
    assert backend.generations==0
    await wrapped.cosens.generate_recipe()
    assert backend.generations==1
    await wrapped.aclose()


@pytest.mark.asyncio
async def test_atomic_feedback_revision_and_recipe_request(tmp_path):
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        task=await a.create_task(source='new_task',objective='Produce output',self_contained=True,key='task')
        execution=await a.start_execution(task['task_id'],key='execution')
        artifact=await a.artifact(name='output.txt',content='final')
        result=await a.submit_execution(execution['execution_id'],request_sha256=execution['request_sha256'],outcome='completed',artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
        await a.record_feedback(result['revision_id'],feedback='Check the format',source='human',key='feedback')
        await a.start_revision(result['revision_id'],key='revision')
        assert backend.generations==0
        await a.start_recipe_candidate(task['task_id'],revision_id=result['revision_id'],key='confirmed')
        assert backend.generations==1
        assert not hasattr(a,'prepare') and not hasattr(a,'start_attribution')


@pytest.mark.asyncio
async def test_recall_memory_and_feedback_source_reach_the_service(tmp_path):
    bodies={}
    def handler(r):
        body=json.loads(r.content or b'{}')
        bodies[r.url.path.rsplit('/',1)[-1]]=body
        if r.url.path.endswith('/executions'):
            return httpx.Response(200,json={'execution_id':'execution_1','request_sha256':'h','status':'awaiting_execution'})
        return httpx.Response(200,json={'feedback_id':'feedback_1','source':body.get('source')})
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(handler)) as sdk:
        await sdk.atoms.start_execution('task_1',key='execution',recall_memory=True)
        await sdk.atoms.record_feedback('revision_1',feedback='score=1.0',source='evaluator',key='feedback')
        with pytest.raises(ValueError, match='source must be one of'):
            await sdk.atoms.record_feedback('revision_1',feedback='x',source='judge',key='bad')
        with pytest.raises(TypeError):
            await sdk.atoms.start_execution('task_1',key='old',include_memory=True)
    assert bodies['executions']=={'recall_memory':True}
    assert bodies['feedback']=={'feedback':'score=1.0','source':'evaluator'}
