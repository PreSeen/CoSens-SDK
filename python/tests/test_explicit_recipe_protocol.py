"""SDK contracts for explicit generation across all three public entry styles."""
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI, AsyncOpenAI
from agents import Agent

from cosens import CoSensClient, cosens


def learning_view(execution_id, status='queued', **fields):
    return {'execution_id': execution_id, 'recipe_id': 'recipe_1', 'version': 1, 'previous_version': None,
            'current': status == 'completed', 'status': status, 'phase': status, 'attribution': None,
            'recipe': {'steps': []} if status == 'completed' else None, 'recipe_sha256': None, 'error': None,
            'error_detail': None, 'models': None, 'memory_context': 'captured', 'initial': True, 'material_gap': None,
            'max_completion_tokens': 200_000, **fields}


class Backend:
    """The execution API: one learning per execution, created by the first learn call."""
    def __init__(self):self.seen=[];self.count=0;self.generations=0;self.learnings={};self.executions=0
    def learning(self,execution_id):
        return self.learnings[execution_id]
    def __call__(self,r):
        body=json.loads(r.content) if r.content else None
        path=r.url.path.removeprefix('/api');self.seen.append((r.method,path,body))
        assert '/runs' not in path and '/improvements' not in path and '/attributions' not in path
        assert '/sessions' not in path and '/revisions' not in path and '/recipe-candidates' not in path
        if path=='/recipes':return httpx.Response(200,json={'items':[],'has_more':False,'next_cursor':None})
        if path=='/v1/memory/recall':return httpx.Response(200,json={'items':[]})
        if path=='/artifacts':
            self.count+=1;return httpx.Response(200,json={'artifact_id':f'artifact_{self.count}','content_hash':'sha256:test'})
        if path=='/tasks' and r.method=='POST':return httpx.Response(200,json={'task_id':'task_1','status':'ready'})
        if path.endswith('/executions') and r.method=='POST':
            self.executions+=1
            execution_id='execution_1' if self.executions==1 else f'execution_{self.executions}'
            kind='correction' if (body or {}).get('previous_execution_id') else 'original'
            return httpx.Response(200,json={'task_id':'task_1','execution_id':execution_id,'kind':kind,'request_sha256':'request-hash','status':'awaiting_execution','request':{'context':{'objective':'task'}}})
        execution_id=path.split('/')[2] if path.startswith('/executions/') else None
        if path.endswith('/result'):
            return httpx.Response(200,json={'task_id':'task_1','execution_id':execution_id,'status':'completed','trace_origin':'original'})
        if path.endswith('/feedback'):
            assert body['source']=='human'
            return httpx.Response(200,json={'feedback_id':'feedback_1','execution_id':execution_id})
        if path.endswith('/learn'):
            assert 'recall_memory' not in body and 'include_memory' not in body
            if execution_id not in self.learnings:
                self.generations+=1
                self.learnings[execution_id]=learning_view(execution_id,max_completion_tokens=body.get('max_completion_tokens',200_000))
            return httpx.Response(200,json=self.learnings[execution_id])
        if path.endswith('/resume'):
            learning=dict(self.learning(execution_id),status='queued')
            if body and 'max_completion_tokens' in body:learning['max_completion_tokens']=body['max_completion_tokens']
            self.learnings[execution_id]=learning
            return httpx.Response(200,json=learning)
        if execution_id and r.method=='GET':
            learning=self.learnings.get(execution_id)
            if learning and learning['status'] in {'queued','running'}:
                learning=self.learnings[execution_id]=dict(learning,status='completed',current=True,recipe={'steps':[]})
            return httpx.Response(200,json={'execution_id':execution_id,'task_id':'task_1','kind':'original','status':'completed','learning':learning})
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
    assert receipt['execution_id']=='execution_1' and receipt['state']=='queued' and backend.generations==1
    assert receipt['learning']['execution_id']=='execution_1' and receipt['action']=='create'
    assert not [key for key in receipt if 'revision' in key or 'candidate' in key]
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
    assert turn['state']=='completed' and turn['execution_id']=='execution_1' and backend.generations==0
    assert not [key for key in turn if 'revision' in key or 'candidate' in key]
    await wrapped.cosens.feedback('Check the format', source='human')
    assert ('POST','/executions/execution_1/feedback') in [entry[:2] for entry in backend.seen]
    assert backend.generations==0
    learning=await wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
    assert backend.generations==1 and learning['execution_id']=='execution_1'
    assert wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)['learning']==learning
    assert backend.seen[-1]==('POST','/executions/execution_1/learn',{'max_completion_tokens':200_000})
    await wrapped.aclose()


@pytest.mark.asyncio
async def test_atomic_feedback_correction_and_learning(tmp_path):
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        task=await a.create_task(source='new_task',objective='Produce output',self_contained=True,key='task')
        execution=await a.start_execution(task['task_id'],key='execution')
        artifact=await a.artifact(name='output.txt',content='final')
        result=await a.submit_execution(execution['execution_id'],request_sha256=execution['request_sha256'],outcome='completed',artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
        assert result['status']=='completed' and 'revision_id' not in result
        await a.record_feedback(result['execution_id'],feedback='Check the format',source='human',key='feedback')
        correction=await a.start_execution(task['task_id'],key='correction',previous_execution_id=result['execution_id'])
        assert correction['kind']=='correction'
        assert backend.seen[-1]==('POST','/tasks/task_1/executions',{'previous_execution_id':'execution_1'})
        assert backend.generations==0
        learning=await a.learn(correction['execution_id'])
        assert backend.generations==1 and learning['execution_id']==correction['execution_id']
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
        await sdk.atoms.record_feedback('execution_1',feedback='score=1.0',source='evaluator',key='feedback')
        with pytest.raises(ValueError, match='source must be one of'):
            await sdk.atoms.record_feedback('execution_1',feedback='x',source='judge',key='bad')
        with pytest.raises(TypeError):
            await sdk.atoms.start_execution('task_1',key='old',include_memory=True)
    assert bodies['executions']=={'recall_memory':True}
    assert bodies['feedback']=={'feedback':'score=1.0','source':'evaluator'}


def test_feedback_on_a_previous_turn_starts_a_correction_of_its_execution(tmp_path):
    backend=Backend()
    def native(r):
        data=json.loads(r.content)
        schema=data.get('response_format',{}).get('json_schema',{})
        if r.url.path.endswith('/chat/completions') and 'feedback' in schema['schema']['properties']:
            text=json.dumps({'activate':True,'reason':'task','feedback':True})
            return httpx.Response(200,json={'id':'decision','object':'chat.completion','created':0,'model':'model','choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]})
        return native_response(r)
    client=OpenAI(api_key='model-key',http_client=httpx.Client(transport=httpx.MockTransport(native)))
    wrapped=cosens(client,mode='auto',api_key='service-key',state_path=tmp_path/'s.sqlite',_http_transport=httpx.MockTransport(backend))
    try:
        first=wrapped.responses.create(model='model',input='Produce a report')
        before=len(backend.seen)
        wrapped.responses.create(model='model',input='The totals are wrong; use the Q3 numbers',previous_response_id=first.id)
        calls=[(m,p,b) for m,p,b in backend.seen[before:] if p.endswith('/feedback') or p.endswith('/executions')]
        assert [call[:2] for call in calls]==[('POST','/executions/execution_1/feedback'),('POST','/tasks/task_1/executions')]
        assert calls[1][2]=={'previous_execution_id':'execution_1'}
        turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['execution_id']=='execution_2' and turn['state']=='completed'
    finally:
        wrapped.close();client.close()


@pytest.mark.asyncio
async def test_start_phases_correct_and_learn(tmp_path):
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        corrected=await sdk.start({'task_id':'task_1','execution_id':'execution_1'},phase='correct',request_key='fix-1')
        assert corrected['execution_id']=='execution_1' and corrected['kind']=='correction'
        assert backend.seen[-1]==('POST','/tasks/task_1/executions',{'previous_execution_id':'execution_1'})
        learning=await sdk.start({'execution_id':'execution_1'},phase='learn',model='public-model')
        assert learning['execution_id']=='execution_1' and learning['status']=='queued'
        assert backend.seen[-1]==('POST','/executions/execution_1/learn',{'model':'public-model'})
        with pytest.raises(ValueError, match='prepare, correct or learn'):
            await sdk.start({'execution_id':'execution_1'},phase='recipe',request_key='k')
