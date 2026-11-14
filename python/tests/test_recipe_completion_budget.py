"""Recipe limits cross public wrapper/atomic boundaries without affecting business calls."""
import json

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI
from cosens import CoSensClient, cosens
from test_explicit_recipe_protocol import Backend, native_response


@pytest.mark.parametrize('value', [True, False, 0, -1, 1.5, '200000', 1_000_001])
def test_invalid_budget_is_rejected_before_http(value, tmp_path):
    backend = Backend()
    client = CoSensClient(api_key='test-key', state_path=tmp_path/'state', _http_transport=httpx.MockTransport(backend))
    try:
        for fn, args, kwargs in [(client.sync_atoms.start_recipe_candidate, ('task',), {'revision_id':'rev','key':'key'}), (client.sync_atoms.resume_recipe_candidate, ('candidate',), {})]:
            with pytest.raises(ValueError, match='max_completion_tokens'):
                fn(*args, **kwargs, max_completion_tokens=value)
        assert backend.seen == []
    finally: client.close()


def test_sync_auto_budget_is_forwarded_and_cached_without_crossing_keys(tmp_path):
    backend = Backend()
    native = OpenAI(api_key='native-key', http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    wrapped = cosens(native, mode='auto', api_key='test-key', state_path=tmp_path/'state', _http_transport=httpx.MockTransport(backend))
    try:
        wrapped.responses.create(model='model',input='Produce a report',max_output_tokens=4096)
        wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
        generated = [b for _,p,b in backend.seen if p.endswith('/recipe-candidates') and b]
        assert generated[-1]['max_completion_tokens'] == 200_000
        wrapped.cosens.generate_recipe()
        assert backend.generations == 1
        with pytest.raises(ValueError, match='request key'):
            wrapped.cosens.generate_recipe(max_completion_tokens=300_000)
        wrapped.cosens.generate_recipe(request_key='new-generation',max_completion_tokens=300_000)
        assert backend.generations == 2
    finally: wrapped.close();native.close()


@pytest.mark.asyncio
async def test_async_auto_and_atoms_forward_completion_budget(tmp_path):
    backend = Backend()
    native = AsyncOpenAI(api_key='native-key', http_client=httpx.AsyncClient(transport=httpx.MockTransport(native_response)))
    wrapped = cosens(native, mode='auto', api_key='test-key', state_path=tmp_path/'state', _http_transport=httpx.MockTransport(backend))
    try:
        await wrapped.responses.create(model='model',input='Produce a report')
        await wrapped.cosens.generate_recipe(max_completion_tokens=250_000)
        generated = [b for _,p,b in backend.seen if p.endswith('/recipe-candidates') and b]
        assert generated[-1]['max_completion_tokens'] == 250_000
    finally: await wrapped.close();await native.close()


@pytest.mark.parametrize('asynchronous',[False,True])
@pytest.mark.asyncio
async def test_atomic_start_resume_wire_and_omission(tmp_path, asynchronous):
    seen=[]
    def response(r):
        seen.append(json.loads(r.content))
        return httpx.Response(200,json={'recipe_candidate_id':'candidate','status':'queued'})
    c=CoSensClient(api_key='test-key',state_path=tmp_path/'state',_http_transport=httpx.MockTransport(response))
    try:
        a=c.atoms if asynchronous else c.sync_atoms
        for fn,args,kwargs in [(a.start_recipe_candidate,('task',),{'revision_id':'rev','key':'first','max_completion_tokens':200_000}), (a.resume_recipe_candidate,('candidate',),{'max_completion_tokens':300_000}), (a.start_recipe_candidate,('task',),{'revision_id':'rev','key':'default'})]:
            value=fn(*args,**kwargs)
            if asynchronous:await value
        assert seen[0]['max_completion_tokens']==200_000
        assert seen[1]['max_completion_tokens']==300_000
        assert 'max_completion_tokens' not in seen[2]
    finally: await c.aclose()


@pytest.mark.parametrize('override',[None,300_000])
def test_auto_resume_keeps_generation_key_budget_and_forwards_override(tmp_path,override):
    class FailedBackend(Backend):
        def __call__(self,r):
            path=r.url.path.removeprefix('/api')
            if path=='/recipe-candidates/candidate_1' and r.method=='GET':
                return httpx.Response(200,json={'recipe_candidate_id':'candidate_1','status':'failed','max_completion_tokens':200_000})
            if path.endswith('/resume'):
                body=json.loads(r.content) if r.content else None
                self.seen.append((r.method,path,body))
                limit=(body or {}).get('max_completion_tokens',200_000)
                return httpx.Response(200,json={'recipe_candidate_id':'candidate_1','status':'queued','max_completion_tokens':limit})
            return super().__call__(r)
    backend=FailedBackend()
    native=OpenAI(api_key='native-key',http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    wrapped=cosens(native,mode='auto',api_key='test-key',state_path=tmp_path/'state',_http_transport=httpx.MockTransport(backend))
    try:
        wrapped.responses.create(model='model',input='Produce output')
        wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
        value=wrapped.cosens.resume_learning(max_completion_tokens=override)
        assert value['max_completion_tokens']==(override or 200_000)
        requests=[b for _,p,b in backend.seen if p.endswith('/resume')]
        assert requests[-1]==({'max_completion_tokens':override} if override else None)
        wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
        assert backend.generations==1
    finally:wrapped.close();native.close()


def test_agent_wrapper_forwards_completion_budget(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from agents import Agent
    backend=Backend()
    class Runner:
        @staticmethod
        async def run(*args,**kwargs):return SimpleNamespace(final_output='final output',new_items=[],interruptions=[])
    async def run():
        agent=Agent(name='writer',instructions='Write output')
        wrapped=cosens(agent,mode='disable',api_key='test-key',state_path=tmp_path/'agent-state',_http_transport=httpx.MockTransport(backend),_runner=Runner)
        try:
            await wrapped.run(agent,'Produce output')
            await wrapped.cosens.generate_recipe(max_completion_tokens=200_000)
            generated=[b for _,p,b in backend.seen if p.endswith('/recipe-candidates') and b]
            assert generated[-1]['max_completion_tokens']==200_000
        finally:await wrapped.aclose()
    asyncio.run(run())
