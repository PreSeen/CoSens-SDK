import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError


async def test_prompt_initialization_and_confirmed_feedback_use_one_task_recipe():
    seen = []
    def server(request):
        seen.append(request)
        if request.url.path.endswith('/tasks'):
            return httpx.Response(200, json={'task_id': 't1', 'status': 'ready'})
        if request.url.path.endswith('/executions'):
            return httpx.Response(200, json={'execution_id': 'e1', 'status': 'awaiting_execution', 'request': {'method': {'recipe_id': 'recipe1', 'version': 2}}})
        return httpx.Response(200, json={'status': 'queued', 'recipe_candidate_id': 'v2'})
    config = {'base_url': 'https://model.example/v1', 'model': 'm', 'api_key': 'model-key'}
    async with CoSensClient(api_key='gateway-key', _http_transport=httpx.MockTransport(server)) as client:
        started = await client.start('请核对本期到账', request_key='task', model_api=config, recipe_id='recipe1')
        assert started.execution_request['method']['version'] == 2
        await client.atoms.improve_task('t1', revision_id='revision1', feedback='重复到账', key='feedback1')
        await client.atoms.select_cause('attribution1', custom_reason='应先按回执号去重')
        await client.atoms.get_task_recipe('t1')
        await client.atoms.get_recipe_versions('recipe1')
    assert json.loads(seen[0].content)['objective'] == '请核对本期到账'
    assert json.loads(seen[1].content) == {'recipe_id': 'recipe1', 'model_api': config}
    assert seen[2].url.path.endswith('/tasks/t1/improvements')
    assert json.loads(seen[2].content) == {'revision_id': 'revision1', 'feedback': '重复到账'}
    assert seen[2].headers['Idempotency-Key'] == 'feedback1'
    assert json.loads(seen[3].content) == {'data': {'custom_reason': '应先按回执号去重'}}
    assert seen[4].url.path.endswith('/tasks/t1/recipe')
    assert seen[5].url.path.endswith('/recipe-chains/recipe1/versions')


@pytest.mark.parametrize('operation', ['start_execution', 'improve_task'])
async def test_task_model_key_is_redacted_from_gateway_errors(operation):
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json={'error': {'message': 'Rejected task-secret', 'code': 'task-secret'}}))
    config = {'base_url': 'https://model.example/v1', 'model': 'm', 'api_key': 'task-secret'}
    async with CoSensClient(api_key='gateway-key', _http_transport=transport) as client:
        with pytest.raises(RSIHTTPError) as error:
            if operation == 'start_execution':
                await client.atoms.start_execution('t1', key='run', model_api=config)
            else:
                await client.atoms.improve_task('t1', revision_id='r1', feedback='missing', key='feedback', model_api=config)
    assert 'task-secret' not in str(error.value)


def test_sync_feedback_and_custom_cause_use_same_endpoints():
    requests = []
    def server(request):
        requests.append(request)
        return httpx.Response(200, json={'status': 'queued'})
    client = CoSensClient(api_key='gateway-key', _http_transport=httpx.MockTransport(server))
    try:
        client.sync_atoms.improve_task('t1', revision_id='r1', feedback='缺少数据', key='feedback')
        client.sync_atoms.select_cause('attribution1', custom_reason='读取范围不完整')
        client.sync_atoms.get_task_recipe('t1')
    finally:
        client.close()
    assert requests[0].url.path.endswith('/tasks/t1/improvements')
    assert json.loads(requests[1].content) == {'data': {'custom_reason': '读取范围不完整'}}


async def test_explicit_region_is_a_parameter_and_does_not_mutate_environment(monkeypatch):
    import os
    monkeypatch.setenv('COSENS_REGION', 'cn')
    requests = []
    def server(request):
        requests.append(request)
        return httpx.Response(200, json={'task_id': 't1', 'status': 'awaiting_execution'})
    async with CoSensClient(api_key='key', region='us', _http_transport=httpx.MockTransport(server)) as client:
        await client.atoms.get_task_recipe('t1')
    assert requests[0].url.host == 'rsi.cosens.ai'
    assert os.environ['COSENS_REGION'] == 'cn'
