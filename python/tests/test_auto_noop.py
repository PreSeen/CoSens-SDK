"""Declined auto turns preserve native requests without unrecorded context reuse."""
import json

import httpx
from openai import AsyncOpenAI, OpenAI
import pytest

from cosens import cosens
from test_explicit_recipe_protocol import native_response


def decision(*args, **kwargs):
    return {'activate': False, 'objective': 'Return OK', 'task_family': 'general', 'reason': 'routine'}


def fixture(api):
    requests, algorithm_calls = [], []

    def model(request):
        requests.append(json.loads(request.content))
        if api == 'responses':
            return native_response(request)
        return httpx.Response(200, json={'id': 'chat_1', 'object': 'chat.completion', 'created': 1,
            'model': 'model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'OK'}, 'finish_reason': 'stop'}]})

    def algorithm(request):
        algorithm_calls.append(request.url.path)
        raise AssertionError('A declined turn must not recall or write remote context')

    body = {'model': 'model', 'temperature': 0.2}
    if api == 'responses':
        body.update(input='Return OK', instructions='Preserve this instruction', store=False)
    else:
        body.update(messages=[{'role': 'system', 'content': 'Preserve this instruction'},
                              {'role': 'user', 'content': 'Return OK'}])
    return model, algorithm, body, requests, algorithm_calls


def assert_noop(wrapped, body, requests, algorithm_calls):
    assert requests == [body]
    assert algorithm_calls == []
    turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
    assert turn['decision']['activate'] is False
    assert turn['capture'] == 'skipped' and turn['state'] == 'completed'
    for field in ('used_recipe_id', 'execution_id', 'revision_id', 'frozen_context'):
        assert not turn.get(field)


@pytest.mark.parametrize('api', ['responses', 'chat'])
def test_sync_noop_preserves_request_without_any_remote_context(api, tmp_path, monkeypatch):
    monkeypatch.setattr('cosens.openai_proxy.decide_sync', decision)
    model, algorithm, body, requests, calls = fixture(api)
    native = OpenAI(api_key='fixture-model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='fixture-service-key', state_path=tmp_path/'turn.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        method = wrapped.responses.create if api == 'responses' else wrapped.chat.completions.create
        method(**body)
        assert_noop(wrapped, body, requests, calls)
    finally:
        wrapped.close()
        native.close()


@pytest.mark.parametrize('api', ['responses', 'chat'])
@pytest.mark.asyncio
async def test_async_noop_preserves_request_without_any_remote_context(api, tmp_path, monkeypatch):
    async def decide(*args, **kwargs):
        return decision()
    monkeypatch.setattr('cosens.openai_proxy.decide_async', decide)
    model, algorithm, body, requests, calls = fixture(api)
    native = AsyncOpenAI(api_key='fixture-model-key', http_client=httpx.AsyncClient(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='fixture-service-key', state_path=tmp_path/'turn.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        method = wrapped.responses.create if api == 'responses' else wrapped.chat.completions.create
        await method(**body)
        assert_noop(wrapped, body, requests, calls)
    finally:
        await wrapped.close()
        await native.close()


@pytest.mark.parametrize('legacy_context', [False, True])
def test_noop_tool_continuation_keeps_original_turn_and_native_request(tmp_path, monkeypatch, legacy_context):
    decisions, requests, calls = [], [], []
    def decide(*args, **kwargs):
        decisions.append(True)
        return decision()
    monkeypatch.setattr('cosens.openai_proxy.decide_sync', decide)

    def model(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, json={'id': 'response_1', 'object': 'response', 'created_at': 1,
                'status': 'completed', 'model': 'model', 'output': [{'type': 'function_call', 'id': 'function_1',
                'call_id': 'call_1', 'name': 'check', 'arguments': '{}', 'status': 'completed'}]})
        return native_response(request)

    def algorithm(request):
        calls.append(request.url.path)
        raise AssertionError('A noop continuation must not access remote context')

    native = OpenAI(api_key='fixture-model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='fixture-service-key', state_path=tmp_path/'turn.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    first = {'model': 'model', 'input': 'Return OK', 'tools': [{'type': 'function', 'name': 'check',
             'parameters': {'type': 'object', 'properties': {}}}]}
    second = {'model': 'model', 'previous_response_id': 'response_1',
              'input': [{'type': 'function_call_output', 'call_id': 'call_1', 'output': 'OK'}]}
    try:
        wrapped.responses.create(**first)
        turn_id = wrapped.cosens.last_turn_id
        if legacy_context:
            runtime = wrapped.cosens._runtime
            old = runtime.turns.get(turn_id)
            old.update(used_recipe_id='legacy-recipe', used_recipe_version=1,
                       used_recipe_candidate_id='legacy-candidate', frozen_context={'recipe': {'status': 'matched'}})
            runtime._save(old)
        wrapped.responses.create(**second)
        assert requests == [first, second] and calls == []
        assert len(decisions) == 1 and wrapped.cosens.last_turn_id == turn_id
        turn = wrapped.cosens.get_turn(turn_id)
        assert turn['state'] == 'completed' and not turn.get('revision_id')
        assert not turn.get('used_recipe_id') and not turn.get('frozen_context')
    finally:
        wrapped.close()
        native.close()
