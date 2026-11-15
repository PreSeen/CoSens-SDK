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
            'max_completion_tokens': 32_000, **fields}


def invalid(message):
    return httpx.Response(422,json={'error':{'code':'invalid_request','message':message}})


def not_found(message):
    return httpx.Response(404,json={'error':{'code':'resource_not_found','message':message}})


class Backend:
    """The execution API as cosens-rsi serves it: a task is created once per key, each Recipe belongs to one
    task, fresh execution IDs per key, one learning per execution.

    ``seen`` holds (method, path, body); ``headers`` holds each request's headers in the same order.
    """
    ACTIVE={'queued','running','awaiting_previous_version','completed'}
    RESUMABLE={'failed','interrupted','awaiting_runner'}

    def __init__(self):
        self.seen=[];self.headers=[];self.count=0;self.generations=0;self.learnings={};self.executions={};self.keys={}
        self.feedback_keys={};self.tasks={};self.task_keys={}
        # Recipe version views {recipe_id, origin, version, task_id, recipe, ...} served by GET /recipes and
        # /recipes/{id}; a generated Recipe's task_id names the one task that owns it.
        self.recipes=[]
        self.task_models={}

    def learning(self,execution_id):
        return self.learnings[execution_id]

    def add_task(self,objective,artifacts=()):
        """A task as POST /tasks stores it; tests also call it for a task made before the test, such as a Recipe's."""
        task_id=f'task_{len(self.tasks)+1}'
        self.tasks[task_id]={'task_id':task_id,'objective':objective,'context_version':1,
                             'artifacts':[{'artifact_id':a,'role':'reference'} if isinstance(a,str) else a for a in artifacts]}
        return task_id

    def _create_task(self,body,key):
        """POST /tasks: one task per idempotency key; the same key with another body is refused."""
        if set(body)-{'objective','artifacts'} or not body.get('objective'):
            return invalid('unknown task fields')
        if key in self.task_keys:
            if self.task_keys[key][0]!=body:
                return invalid('idempotency key reused with different task')
            return self.tasks[self.task_keys[key][1]]
        task_id=self.add_task(body['objective'],body.get('artifacts',[]))
        self.task_keys[key]=(body,task_id)
        return self.tasks[task_id]

    def _method(self,recipe_id):
        recipe=next(r for r in self._current_recipes() if r['recipe_id']==recipe_id)
        return {'recipe_id':recipe_id,'version':recipe['version'],'recipe_sha256':'f'*64,'recipe':recipe['recipe']}

    def _task_method(self,task_id,recipe_id):
        """The method a new execution freezes: the named Recipe of this task, else the task's only Recipe."""
        owned=sorted({r['recipe_id'] for r in self.recipes if r.get('task_id')==task_id})
        if recipe_id is not None:
            return self._method(recipe_id) if recipe_id in owned else not_found('Recipe not found in this task')
        if len(owned)>1:
            return httpx.Response(422,json={'error':{'code':'recipe_choice_required','recipe_ids':owned,
                                                     'message':'the task has several Recipes; name one with recipe_id'}})
        return self._method(owned[0]) if owned else None

    @staticmethod
    def _input(source,request_input):
        """The frozen input and what it lacks; a reconstruction also needs what it started from and what it produced."""
        default='original_input' if source=='new_task' else 'submitted_output'
        artifacts=[{'artifact_id':a,'role':default} if isinstance(a,str) else a for a in request_input.get('artifacts',[])]
        value={'objective':request_input.get('objective'),'artifacts':artifacts,'self_contained':bool(request_input.get('self_contained'))}
        missing=[] if value['objective'] else ['objective']
        if source=='external_artifact':
            roles={a['role'] for a in artifacts}
            if 'original_input' not in roles and not value['self_contained']:
                missing.append('original_input_or_self_contained_task')
            if 'submitted_output' not in roles:
                missing.append('submitted_output')
        return value,missing

    def _start(self,body,key):
        """POST /executions: a new execution names task_id, source and input; a correction names previous_execution_id."""
        body=body or {}
        previous=body.get('previous_execution_id')
        if previous is not None:
            if set(body)-{'previous_execution_id','task_id','model'}:
                return invalid('a correction takes only previous_execution_id, task_id and model')
            before=self.executions.get(previous)
            if before is None:
                return not_found('execution not found')
            if before['status']!='completed':
                return invalid('execution has no completed output')
            if body.get('task_id') not in (None,before['task_id']):
                return invalid('previous execution belongs to another task')
            task_id,request_input,instance_id=before['task_id'],before['input'],before['instance_id']
            scope,intent=('correction',previous,key),None
            # A correction follows the current version of the Recipe the previous execution followed.
            method=self._method(before['method']['recipe_id']) if before['method'] else self._task_method(task_id,None)
        else:
            task_id,source,request_input=body.get('task_id'),body.get('source'),body.get('input')
            if not task_id or source not in {'new_task','external_artifact'} or not isinstance(request_input,dict):
                return invalid('a new execution needs task_id, source and input')
            if task_id not in self.tasks:
                return not_found('task not found')
            request_input,missing=self._input(source,request_input)
            if missing:
                return httpx.Response(422,json={'error':{'code':'input_incomplete','message':'the execution input is incomplete','missing':missing}})
            instance_id=None
            scope,intent=('original',task_id,key),(source,json.dumps(request_input,sort_keys=True),body.get('recipe_id'),bool(body.get('recall_memory')))
            method=self._task_method(task_id,body.get('recipe_id'))
        if isinstance(method,httpx.Response):
            return method
        frozen=self._freeze_model(task_id,body.get('model'))
        if frozen is not None:
            return frozen
        if scope in self.keys:
            if self.keys[scope][0]!=intent:
                return invalid('idempotency key reused with different context')
            return self.keys[scope][1]
        execution_id=f'execution_{len(self.executions)+1}'
        kind='correction' if previous else 'original'
        instance_id=instance_id or execution_id
        self.executions[execution_id]={'task_id':task_id,'kind':kind,'status':'awaiting_execution','previous_execution_id':previous,
                                       'request_sha256':'sha-'+execution_id,'input':request_input,'instance_id':instance_id,
                                       'method':method}
        task=self.tasks[task_id]
        request={'task_context':{'objective':task['objective'],'artifacts':task['artifacts'],'context_version':task['context_version']},
                 'input':request_input,'method':method}
        receipt={'task_id':task_id,'execution_id':execution_id,'instance_id':instance_id,'kind':kind,
                 'request_sha256':'sha-'+execution_id,'status':'awaiting_execution','request':request}
        self.keys[scope]=(intent,receipt)
        return receipt

    def _freeze_model(self,task_id,model):
        """The task's model is frozen once set; a different one is rejected."""
        if model is None:
            return None
        if self.task_models.setdefault(task_id,model)!=model:
            return invalid('task model is frozen; only its credential and auth type may rotate')
        return None

    def _current_recipes(self):
        latest={}
        for recipe in self.recipes:
            if recipe['version']>latest.get(recipe['recipe_id'],{}).get('version',0):
                latest[recipe['recipe_id']]=recipe
        return list(latest.values())

    def _recipe_lookup(self,path,params):
        summary=lambda r:{'recipe_id':r['recipe_id'],'origin':r['origin'],'version':r['version'],'title':r['recipe'].get('title'),'task_id':r.get('task_id'),'updated_at':'t'}
        if path=='/recipes':
            if 'q' in params:
                if 'cursor' in params or 'origin' in params:
                    return invalid('search takes no cursor or origin')
                found=[r for r in self._current_recipes() if params['q'].lower() in str(r['recipe'].get('title','')).lower()]
                return httpx.Response(200,json={'items':[summary(r) for r in found[:int(params.get('limit',10))]]})
            items=[r for r in self._current_recipes() if not params.get('origin') or r['origin']==params['origin']]
            limit=int(params.get('limit',20))
            return httpx.Response(200,json={'items':[summary(r) for r in items[:limit]],'has_more':len(items)>limit,'next_cursor':None})
        recipe_id=path.split('/')[2]
        if 'version' in params:
            found=[r for r in self.recipes if r['recipe_id']==recipe_id and r['version']==int(params['version'])]
        else:
            found=[r for r in self._current_recipes() if r['recipe_id']==recipe_id]
        return httpx.Response(200,json={'task_id':None,**found[0]}) if found else not_found('Recipe not found')

    def _learn(self,execution_id,body):
        budget=(body or {}).get('max_completion_tokens')
        if execution_id in self.learnings:
            current=self.learnings[execution_id]
            if budget is not None and budget!=current['max_completion_tokens']:
                return invalid('this execution already learned with a different completion budget')
            return current
        if self.executions.get(execution_id,{}).get('status')!='completed':
            return invalid('execution has not completed')
        self.generations+=1
        self.learnings[execution_id]=learning_view(execution_id,max_completion_tokens=budget or 32_000)
        return self.learnings[execution_id]

    def _feedback(self,execution_id,execution,body,key):
        if execution['status']!='completed':return invalid('feedback needs a completed execution')
        value={'feedback_id':f'feedback_{len(self.feedback_keys)+1}','execution_id':execution_id,'task_id':execution['task_id'],
               'message':body['feedback'],'source':body['source']}
        saved=self.feedback_keys.get((execution_id,key))
        if saved is not None:
            if (saved['message'],saved['source'])!=(body['feedback'],body['source']):
                return invalid('idempotency key reused with different feedback')
            return saved
        self.feedback_keys[(execution_id,key)]=value
        return value

    def _resume(self,execution_id,body):
        budget=(body or {}).get('max_completion_tokens')
        current=self.learnings.get(execution_id)
        if current is None:
            return invalid('this execution has not learned yet')
        if current['status'] in self.ACTIVE:
            if budget is not None and budget!=current['max_completion_tokens']:
                return invalid('cannot change the completion budget of an active or completed learning')
            return current
        if current['status'] not in self.RESUMABLE:
            return invalid(f"a learning in {current['status']} cannot be resumed")
        current=dict(current,status='queued',error=None,error_detail=None)
        if budget is not None:current['max_completion_tokens']=budget
        self.learnings[execution_id]=current
        return current

    def __call__(self,r):
        body=json.loads(r.content) if r.content else None
        path=r.url.path.removeprefix('/api');self.seen.append((r.method,path,body));self.headers.append(dict(r.headers))
        assert '/runs' not in path and '/improvements' not in path and '/attributions' not in path
        assert '/sessions' not in path and '/revisions' not in path and '/recipe-candidates' not in path
        if path=='/recipes' or (path.startswith('/recipes/') and r.method=='GET'):
            return self._recipe_lookup(path,dict(r.url.params))
        if path=='/v1/memory/recall':return httpx.Response(200,json={'items':[]})
        if path=='/artifacts':
            self.count+=1;return httpx.Response(200,json={'artifact_id':f'artifact_{self.count}','content_hash':'sha256:test'})
        if path.startswith('/tasks/') and r.method=='GET':
            task=self.tasks.get(path.split('/')[2])
            if task is None:
                return not_found('task not found')
            executions=[{'execution_id':key,'instance_id':e['instance_id'],'kind':e['kind'],'status':e['status'],
                         'request_sha256':e['request_sha256']} for key,e in self.executions.items() if e['task_id']==task['task_id']]
            recipes=[{'recipe_id':r['recipe_id'],'current_version':r['version'],'latest_version':r['version'],'learning':None}
                     for r in self._current_recipes() if r.get('task_id')==task['task_id']]
            return httpx.Response(200,json={**task,'executions':executions,'recipes':recipes})
        if path=='/tasks' and r.method=='POST':
            answer=self._create_task(body,r.headers.get('idempotency-key'))
            return answer if isinstance(answer,httpx.Response) else httpx.Response(200,json=answer)
        if path=='/executions' and r.method=='POST':
            answer=self._start(body,r.headers.get('idempotency-key'))
            return answer if isinstance(answer,httpx.Response) else httpx.Response(200,json=answer)
        execution_id=path.split('/')[2] if path.startswith('/executions/') else None
        execution=self.executions.get(execution_id)
        if execution is None:
            return httpx.Response(404,json={'error':{'code':'not_found','message':'execution not found'}})
        if path.endswith('/result'):
            if body['request_sha256']!=execution['request_sha256']:return invalid('execution request hash mismatch')
            if execution['status']=='awaiting_execution':execution['status']=body['outcome']
            return httpx.Response(200,json={'task_id':execution['task_id'],'execution_id':execution_id,'status':execution['status'],'trace_origin':execution['kind']})
        if path.endswith('/feedback'):
            assert body['source']=='human'
            answer=self._feedback(execution_id,execution,body,r.headers.get('idempotency-key'))
            return answer if isinstance(answer,httpx.Response) else httpx.Response(200,json=answer)
        if path.endswith('/learn') or path.endswith('/resume'):
            assert 'recall_memory' not in (body or {}) and 'include_memory' not in (body or {})
            frozen=self._freeze_model(execution['task_id'],(body or {}).get('model'))
            if frozen is not None:
                return frozen
            answer=(self._learn if path.endswith('/learn') else self._resume)(execution_id,body)
            return answer if isinstance(answer,httpx.Response) else httpx.Response(200,json=answer)
        if r.method=='GET':
            learning=self.learnings.get(execution_id)
            if learning and learning['status'] in {'queued','running'}:
                learning=self.learnings[execution_id]=dict(learning,status='completed',current=True,recipe={'steps':[]})
            return httpx.Response(200,json={'execution_id':execution_id,'task_id':execution['task_id'],'instance_id':execution['instance_id'],'kind':execution['kind'],'status':execution['status'],
                                            'previous_execution_id':execution['previous_execution_id'],'learning':learning})
        raise AssertionError((r.method,path,body))


NEW=dict(source='new_task',objective='Produce output',self_contained=True)


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
    task_body=next(body for method,path,body in backend.seen if path=='/tasks')
    execution_body=next(body for method,path,body in backend.seen if path=='/executions')
    assert set(task_body)=={'objective','artifacts'} and task_body['artifacts']==[]
    assert execution_body['task_id']=='task_1' and execution_body['source']=='new_task'
    assert execution_body['input']['objective']==task_body['objective']
    assert execution_body['input']['artifacts']==[{'artifact_id':'artifact_1','role':'original_input'}]
    assert wrapped.cosens.wait_learning(timeout=0)['state']=='awaiting_confirmation'
    wrapped.cosens.feedback('Check the format', source='human')
    assert backend.generations==0
    receipt=wrapped.cosens.generate_recipe()
    assert receipt['execution_id']=='execution_1' and receipt['state']=='queued' and backend.generations==1
    assert receipt['max_completion_tokens']==32_000 and backend.seen[-1][2]=={}
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
        task=await a.create_task(objective='Produce output',key='task')
        assert task=={'task_id':'task_1','objective':'Produce output','artifacts':[],'context_version':1}
        assert backend.seen[-1]==('POST','/tasks',{'objective':'Produce output','artifacts':[]})
        execution=await a.start_execution(task['task_id'],key='execution',**NEW)
        assert backend.seen[-1]==('POST','/executions',{'task_id':'task_1','source':'new_task',
            'input':{'objective':'Produce output','artifacts':[],'self_contained':True}})
        assert execution['instance_id']==execution['execution_id']
        assert execution['request']['input']=={'objective':'Produce output','artifacts':[],'self_contained':True}
        artifact=await a.artifact(name='output.txt',content='final')
        result=await a.submit_execution(execution['execution_id'],request_sha256=execution['request_sha256'],outcome='completed',artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
        assert result['status']=='completed' and 'revision_id' not in result
        await a.record_feedback(result['execution_id'],feedback='Check the format',source='human',key='feedback')
        correction=await a.start_execution(task['task_id'],key='correction',previous_execution_id=result['execution_id'])
        assert correction['kind']=='correction' and correction['execution_id']!=result['execution_id']
        assert correction['instance_id']==execution['execution_id']
        assert backend.seen[-1]==('POST','/executions',{'task_id':'task_1','previous_execution_id':'execution_1'})
        corrected=await a.artifact(name='corrected.txt',content='fixed')
        done=await a.submit_execution(correction['execution_id'],request_sha256=correction['request_sha256'],outcome='completed',artifacts=[{'artifact_id':corrected['artifact_id'],'role':'submitted_output'}])
        assert done['trace_origin']=='correction'
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
        await sdk.atoms.start_execution('task_1',key='execution',source='new_task',objective='Produce output',recall_memory=True)
        await sdk.atoms.record_feedback('execution_1',feedback='score=1.0',source='evaluator',key='feedback')
        with pytest.raises(ValueError, match='source must be one of'):
            await sdk.atoms.record_feedback('execution_1',feedback='x',source='judge',key='bad')
        with pytest.raises(TypeError):
            await sdk.atoms.start_execution('task_1',key='old',source='new_task',objective='Produce output',include_memory=True)
    assert bodies['executions']=={'task_id':'task_1','source':'new_task','input':{'objective':'Produce output','artifacts':[]},
                                  'recall_memory':True}
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
        calls=[(i,m,p,b) for i,(m,p,b) in enumerate(backend.seen) if i>=before and (p.endswith('/feedback') or p.endswith('/executions'))]
        assert [call[1:3] for call in calls]==[('POST','/executions/execution_1/feedback'),('POST','/executions')]
        assert calls[1][3]=={'task_id':'task_1','previous_execution_id':'execution_1'}
        turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert backend.headers[calls[1][0]]['idempotency-key']=='auto-correction:'+turn['turn_id']
        assert turn['execution_id']=='execution_2' and turn['execution_id']!='execution_1' and turn['state']=='completed'
        assert backend.executions['execution_2']['kind']=='correction'
        assert not [p for _,p,_ in backend.seen[before:] if p.startswith('/recipes')]
    finally:
        wrapped.close();client.close()


async def _completed_execution(sdk):
    prepared=await sdk.start('Produce output',request_key='prepare-1')
    artifact=await sdk.atoms.artifact(name='output.txt',content='final')
    await sdk.atoms.submit_execution(prepared['execution_id'],request_sha256=prepared['request_sha256'],outcome='completed',
                                     artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
    return prepared


@pytest.mark.asyncio
async def test_start_phases_correct_and_learn(tmp_path):
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        prepared=await _completed_execution(sdk)
        snapshot={'task_id':prepared['task_id'],'execution_id':prepared['execution_id']}
        corrected=await sdk.start(snapshot,phase='correct',request_key='fix-1')
        assert corrected['kind']=='correction' and corrected['execution_id']!=prepared['execution_id']
        assert backend.seen[-1]==('POST','/executions',{'task_id':'task_1','previous_execution_id':prepared['execution_id']})
        assert backend.headers[-1]['idempotency-key']=='fix-1'
        learning=await sdk.start(snapshot,phase='learn',model='public-model')
        assert learning['execution_id']==prepared['execution_id'] and learning['status']=='queued'
        assert backend.seen[-1]==('POST',f"/executions/{prepared['execution_id']}/learn",{'model':'public-model'})
        with pytest.raises(ValueError, match='prepare, correct or learn'):
            await sdk.start(snapshot,phase='recipe',request_key='k')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['auto','approval'])
@pytest.mark.parametrize('extra',[{'recipe_id':'recipe_1'},{'recall_memory':True}])
async def test_start_correct_rejects_recipe_and_memory_before_asking_or_sending(tmp_path,extra,mode):
    backend=Backend();asked=[]
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        with pytest.raises(ValueError, match='a correction takes only previous_execution_id, task_id and model'):
            await sdk.start({'task_id':'task_1','execution_id':'execution_1'},phase='correct',request_key='fix-1',
                            mode=mode,authorize=lambda *args:asked.append(args) or True,**extra)
    assert backend.seen==[] and asked==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('spec,message',[
    ({'objective':'Produce output'},'task.source is required'),
    ({'source':'external_artifact','objective':'Produce output'},'external_artifact requires submitted_output'),
    ({'source':'new_task','objective':'Produce output','self_contained':'yes'},'self_contained must be a boolean'),
    ({'source':'new_task'},'objective must be nonempty')])
async def test_start_prepare_validates_the_spec_before_asking_or_creating_a_task(tmp_path,spec,message):
    backend=Backend();asked=[]
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        with pytest.raises(ValueError, match=message):
            await sdk.start({'task':spec},request_key='prepare-1',mode='approval',authorize=lambda *args:asked.append(args) or True)
    assert backend.seen==[] and asked==[] and backend.tasks=={}


def _recipe_view(recipe_id,task_id,version=1,title='Quarterly report',steps=('Use the Q3 numbers',)):
    return {'recipe_id':recipe_id,'origin':'generated','version':version,'task_id':task_id,
            'recipe':{'title':title,'when':'Writing a report','when_not':'Chat','lessons':[],
                      'steps':[{'id':f's{n}','instruction':text} for n,text in enumerate(steps,1)]}}


@pytest.mark.asyncio
async def test_start_runs_a_named_recipe_in_the_task_that_owns_it(tmp_path):
    backend=Backend()
    owner=backend.add_task('Write the quarterly report')
    backend.recipes=[_recipe_view('recipe_1',owner)]
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        prepared=await sdk.start('Produce a report',request_key='prepare-1',recipe_id='recipe_1')
        assert prepared['task_id']==owner and prepared['execution_request']['method']['recipe_id']=='recipe_1'
        assert prepared['execution_request']['task_context']['objective']=='Write the quarterly report'
        assert [entry[:2] for entry in backend.seen]==[('GET','/recipes/recipe_1'),('GET',f'/tasks/{owner}'),('POST','/executions')]
        assert backend.seen[-1][2]['task_id']==owner and backend.seen[-1][2]['recipe_id']=='recipe_1'
        other=backend.add_task('Write the monthly report')
        before=len(backend.seen)
        with pytest.raises(ValueError, match=f'Recipe recipe_1 belongs to task {owner}, not {other}'):
            await sdk.start({'task':{'source':'new_task','objective':'Produce a report'},'task_id':other},
                            request_key='prepare-2',recipe_id='recipe_1')
        assert [entry[:2] for entry in backend.seen[before:]]==[('GET','/recipes/recipe_1')]
        backend.recipes.append({'recipe_id':'team','origin':'uploaded','version':1,'recipe':{'title':'Team'}})
        with pytest.raises(ValueError, match='Recipe team belongs to no task'):
            await sdk.start('Produce a report',request_key='prepare-3',recipe_id='team')
    assert len(backend.tasks)==2 and not [path for _,path,_ in backend.seen if path=='/tasks']


def _policy(match):
    """A decision model that activates every turn and answers the Recipe matcher with ``match``."""
    def native(r):
        if r.url.path.endswith('/chat/completions'):
            schema=json.loads(r.content)['response_format']['json_schema']
            answer={'recipe_id':match} if schema['name']=='cosens_recipe_match' else {'activate':True,'reason':'task'}
            return httpx.Response(200,json={'id':'decision','object':'chat.completion','created':0,'model':'model','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(answer)},'finish_reason':'stop'}]})
        return native_response(r)
    return native


def test_auto_runs_a_matched_recipe_in_the_task_that_owns_it(tmp_path):
    """Reusing a Recipe creates no task: the execution goes to the Recipe's task and keeps that task's model."""
    backend=Backend()
    owner=backend.add_task('Write the quarterly report')
    backend.task_models[owner]='owner-model'
    backend.recipes=[_recipe_view('recipe_1',owner)]
    client=OpenAI(api_key='model-key',http_client=httpx.Client(transport=httpx.MockTransport(_policy('recipe_1'))))
    wrapped=cosens(client,mode='auto',api_key='service-key',state_path=tmp_path/'s.sqlite',_http_transport=httpx.MockTransport(backend))
    try:
        wrapped.responses.create(model='model',input='Produce a report')
        assert not [path for _,path,_ in backend.seen if path=='/tasks'] and list(backend.tasks)==[owner]
        execution=next(body for _,path,body in backend.seen if path=='/executions')
        assert execution['task_id']==owner and execution['recipe_id']=='recipe_1' and 'model' not in execution
        turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['state']=='completed' and turn['task_id']==owner
        assert (turn['used_recipe_id'],turn['used_recipe_version'])==('recipe_1',1)
        assert turn['execution_request']['task_context']['objective']=='Write the quarterly report'
        # A turn that matches nothing makes its own task and names the model.
        backend.recipes=[]
        wrapped.responses.create(model='model',input='Produce another report')
        assert [body for _,path,body in backend.seen if path=='/tasks']==[{'objective':'Produce another report','artifacts':[]}]
        assert [body for _,path,body in backend.seen if path=='/executions'][-1]['model']=='model'
    finally:
        wrapped.close();client.close()


@pytest.mark.asyncio
async def test_the_fake_keeps_recipes_in_their_task_and_says_what_is_missing(tmp_path):
    from cosens.errors import RSIHTTPError
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        weekly=await a.create_task(objective='Weekly report',key='weekly')
        assert await a.create_task(objective='Weekly report',key='weekly')==weekly
        with pytest.raises(RSIHTTPError, match='different task') as caught:
            await a.create_task(objective='Monthly report',key='weekly')
        assert caught.value.status_code==422
        monthly=await a.create_task(objective='Monthly report',key='monthly')
        assert monthly['task_id']!=weekly['task_id'] and len(backend.tasks)==2
        backend.recipes=[_recipe_view('weekly_recipe',weekly['task_id']),_recipe_view('monthly_recipe',monthly['task_id'])]
        with pytest.raises(RSIHTTPError) as caught:
            await a.start_execution(weekly['task_id'],key='e1',recipe_id='monthly_recipe',**NEW)
        assert caught.value.status_code==404
        started=await a.start_execution(weekly['task_id'],key='e1',**NEW)
        assert started['request']['method']['recipe_id']=='weekly_recipe'
        assert started['request']['task_context']=={'objective':'Weekly report','artifacts':[],'context_version':1}
        backend.recipes.append(_recipe_view('weekly_short',weekly['task_id']))
        with pytest.raises(RSIHTTPError) as caught:
            await a.start_execution(weekly['task_id'],key='e2',**NEW)
        assert caught.value.code=='recipe_choice_required' and caught.value.recipe_ids==['weekly_recipe','weekly_short']
        with pytest.raises(RSIHTTPError) as caught:
            await a.start_execution(weekly['task_id'],key='e3',source='external_artifact',artifacts=['output'])
        assert caught.value.code=='input_incomplete' and caught.value.missing==['objective','original_input_or_self_contained_task']
    assert Backend._input('external_artifact',{'objective':'x'})[1]==['original_input_or_self_contained_task','submitted_output']


@pytest.mark.asyncio
async def test_agent_wrapper_surfaces_a_changed_learning_budget(tmp_path):
    from cosens.errors import RSIHTTPError
    backend=Backend()
    class Runner:
        @staticmethod
        async def run(agent,user_input,**options):return SimpleNamespace(final_output='agent artifact',new_items=[],interruptions=[])
    agent=Agent(name='reporter',instructions='Produce output')
    wrapped=cosens(agent,mode='disable',api_key='service-key',state_path=tmp_path/'agent.sqlite',_http_transport=httpx.MockTransport(backend),_runner=Runner)
    try:
        await wrapped.run(agent,'Produce a report')
        await wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
        assert (await wrapped.cosens.generate_recipe())['max_completion_tokens']==200_000
        with pytest.raises(RSIHTTPError) as caught:
            await wrapped.cosens.generate_recipe(max_completion_tokens=300_000)
        assert caught.value.status_code==422 and caught.value.code=='invalid_request'
        assert backend.generations==1
    finally:
        await wrapped.aclose()


@pytest.mark.asyncio
async def test_feedback_and_resume_follow_the_service_rules(tmp_path):
    from cosens.errors import RSIHTTPError
    backend=Backend()
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        pending=await sdk.start('Produce output',request_key='prepare-1')
        with pytest.raises(RSIHTTPError) as caught:
            await a.record_feedback(pending['execution_id'],feedback='f',source='human',key='early')
        assert caught.value.status_code==422
        artifact=await a.artifact(name='output.txt',content='final')
        await a.submit_execution(pending['execution_id'],request_sha256=pending['request_sha256'],outcome='completed',
                                 artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}])
        with pytest.raises(RSIHTTPError, match='has not learned yet'):
            await a.resume_learning(pending['execution_id'])
        learning=await a.learn(pending['execution_id'],max_completion_tokens=200_000)
        assert await a.resume_learning(pending['execution_id'])==learning
        with pytest.raises(RSIHTTPError, match='completion budget'):
            await a.resume_learning(pending['execution_id'],max_completion_tokens=300_000)


def test_a_correction_uses_the_recipe_the_service_binds_from_the_task(tmp_path):
    """The task already has a Recipe: the feedback turn makes no lookup and injects the correction's method."""
    stale={'title':'Old report','when':'Writing a report','when_not':'Chat','steps':[{'id':'s1','instruction':'Old step'}],'lessons':[]}
    head={'title':'Quarterly report','when':'Writing a report','when_not':'Chat','steps':[{'id':'s1','instruction':'Use the Q3 numbers'}],'lessons':[]}
    backend=Backend();responses=[]
    # The turn's own task (task_1, the first one created) owns both versions; the matcher declines both.
    backend.recipes=[{'recipe_id':'recipe_1','origin':'generated','version':1,'task_id':'task_1','recipe':stale},
                     {'recipe_id':'recipe_1','origin':'generated','version':2,'task_id':'task_1','recipe':head}]
    def native(r):
        data=json.loads(r.content)
        if r.url.path.endswith('/chat/completions'):
            schema=data['response_format']['json_schema']
            if schema['name']=='cosens_recipe_match':
                answer={'recipe_id':''}
            else:
                answer={'activate':True,'reason':'task',**({'feedback':True} if 'feedback' in schema['schema']['properties'] else {})}
            return httpx.Response(200,json={'id':'decision','object':'chat.completion','created':0,'model':'model','choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(answer)},'finish_reason':'stop'}]})
        responses.append(data)
        return native_response(r)
    client=OpenAI(api_key='model-key',http_client=httpx.Client(transport=httpx.MockTransport(native)))
    wrapped=cosens(client,mode='auto',api_key='service-key',state_path=tmp_path/'s.sqlite',_http_transport=httpx.MockTransport(backend))
    try:
        first=wrapped.responses.create(model='model',input='Produce a report')
        before=len(backend.seen)
        wrapped.responses.create(model='model',input='The totals are wrong; use the Q3 numbers',previous_response_id=first.id)
        assert not [p for _,p,_ in backend.seen[before:] if p.startswith('/recipes')]
        turn=wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['state']=='completed' and turn['execution_id']=='execution_2'
        assert (turn['used_recipe_id'],turn['used_recipe_version'])==('recipe_1',2)
        assert 'Use the Q3 numbers' in responses[-1]['instructions'] and 'Old step' not in responses[-1]['instructions']
    finally:
        wrapped.close();client.close()


@pytest.mark.asyncio
async def test_execution_keys_feedback_keys_and_request_hashes_follow_the_service(tmp_path):
    from cosens.errors import RSIHTTPError
    backend=Backend()
    backend.add_task('Produce output')
    backend.recipes=[{'recipe_id':'recipe_1','origin':'generated','version':3,'task_id':'task_1','recipe':{'title':'Head','steps':[]}}]
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        first=await a.start_execution('task_1',key='same',**NEW)
        assert first['request']['method']['version']==3
        assert (await a.start_execution('task_1',key='same',**NEW))['execution_id']==first['execution_id']
        for changed in ({'recall_memory':True},{'objective':'Another week'}):
            with pytest.raises(RSIHTTPError, match='different context') as caught:
                await a.start_execution('task_1',key='same',**{**NEW,**changed})
            assert caught.value.status_code==422
        with pytest.raises(RSIHTTPError, match='request hash mismatch'):
            await a.submit_execution(first['execution_id'],request_sha256='stale',outcome='completed',artifacts=[{'artifact_id':'a1','role':'submitted_output'}])
        await a.submit_execution(first['execution_id'],request_sha256=first['request_sha256'],outcome='completed',artifacts=[{'artifact_id':'a1','role':'submitted_output'}])
        second=await a.start_execution('task_1',key='other',**NEW)
        await a.submit_execution(second['execution_id'],request_sha256=second['request_sha256'],outcome='completed',artifacts=[{'artifact_id':'a2','role':'submitted_output'}])
        # A correction key is scoped to the execution it corrects.
        fix_first=await a.start_execution('task_1',key='fix',previous_execution_id=first['execution_id'])
        fix_second=await a.start_execution('task_1',key='fix',previous_execution_id=second['execution_id'])
        assert len({fix_first['execution_id'],fix_second['execution_id'],first['execution_id'],second['execution_id']})==4
        assert fix_first['request']['method']['version']==3
        for task_id,previous,status in (('task_1','execution_99',404),('task_2',first['execution_id'],422)):
            with pytest.raises(RSIHTTPError) as caught:
                await a.start_execution(task_id,key='fix',previous_execution_id=previous)
            assert caught.value.status_code==status
        await a.record_feedback(first['execution_id'],feedback='Too long',source='human',key='fb')
        assert (await a.record_feedback(first['execution_id'],feedback='Too long',source='human',key='fb'))['message']=='Too long'
        with pytest.raises(RSIHTTPError, match='different feedback') as caught:
            await a.record_feedback(first['execution_id'],feedback='Too short',source='human',key='fb')
        assert caught.value.status_code==422


@pytest.mark.asyncio
async def test_the_fake_freezes_the_task_model_and_looks_recipes_up_like_the_service(tmp_path):
    from cosens.errors import RSIHTTPError
    backend=Backend()
    view=lambda version,origin='generated',recipe_id='recipe_1':{'recipe_id':recipe_id,'origin':origin,'version':version,'recipe':{'title':f'Report v{version}'}}
    backend.recipes=[view(1),view(2),view(1,'uploaded','team')]
    backend.add_task('Produce output')
    async with CoSensClient(api_key='service-key',state_path=tmp_path/'atoms.sqlite',_http_transport=httpx.MockTransport(backend)) as sdk:
        a=sdk.atoms
        first=await a.start_execution('task_1',key='e1',model='m1',**NEW)
        with pytest.raises(RSIHTTPError, match='frozen') as caught:
            await a.start_execution('task_1',key='e2',model='m2',**NEW)
        assert caught.value.status_code==422
        await a.submit_execution(first['execution_id'],request_sha256=first['request_sha256'],outcome='completed',artifacts=[{'artifact_id':'a1','role':'submitted_output'}])
        with pytest.raises(RSIHTTPError, match='frozen'):
            await a.learn(first['execution_id'],model='m2')
        await a.learn(first['execution_id'],model='m1')
        with pytest.raises(RSIHTTPError, match='frozen'):
            await a.resume_learning(first['execution_id'],model='m2')
        assert (await a.get_recipe('recipe_1'))['version']==2
        assert (await a.get_recipe('recipe_1',version=1))['version']==1
        for call in (a.get_recipe('recipe_1',version=9),a.get_recipe('missing')):
            with pytest.raises(RSIHTTPError) as caught:
                await call
            assert caught.value.status_code==404
        assert [(r['recipe_id'],r['version']) for r in (await a.list_recipes(limit=100,origin='generated'))['items']]==[('recipe_1',2)]
        assert [r['recipe_id'] for r in (await a.search_recipes('report v2'))['items']]==['recipe_1']
        assert (await a.search_recipes('nothing'))['items']==[]
