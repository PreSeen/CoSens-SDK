"""A failed auto decision must leave the native request usable."""
import httpx
import pytest

from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from test_explicit_recipe_protocol import native_response


def _gateway(request):
    if request.url.path.endswith('/chat/completions'):
        raise httpx.ReadTimeout('decision timed out')
    return native_response(request)


def _unexpected_algorithm(request):
    raise AssertionError(f'Auto fallback must not call CoSens: {request.url.path}')


@pytest.mark.parametrize('configured_timeout,expected', [(None, 5), (12, 12)])
def test_auto_decision_timeout_configuration(monkeypatch, tmp_path, configured_timeout, expected):
    import cosens.openai_proxy as proxy
    seen = []

    def decision(native, model, text, tools, **options):
        seen.append(options['timeout'])
        return {'activate': False, 'objective': text, 'task_family': 'general', 'reason': 'routine'}

    monkeypatch.setattr(proxy, 'decide_sync', decision)
    def algorithm(request):
        if request.url.path.endswith('/recipe-candidates'):
            return httpx.Response(200, json={'items': [], 'has_more': False})
        if request.url.path.endswith('/memory/recall'):
            return httpx.Response(200, json={'items': []})
        return _unexpected_algorithm(request)

    native = OpenAI(base_url='https://llm.cosens.ai/v1', api_key='test-only-gateway-key',
        http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    options = {} if configured_timeout is None else {'decision_timeout': configured_timeout}
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key',
        state_path=tmp_path/'default.sqlite', _http_transport=httpx.MockTransport(algorithm), **options)
    try:
        assert wrapped.responses.create(model='model', input='Produce a report').output_text == 'final artifact'
        assert seen == [expected]
    finally:
        wrapped.close(); native.close()


def test_auto_recipe_match_timeout_returns_native_response(tmp_path):
    def gateway(request):
        if request.url.path.endswith('/chat/completions'):
            import json
            body = json.loads(request.content)
            name = body['response_format']['json_schema']['name']
            if name == 'cosens_recipe_match':
                raise httpx.ReadTimeout('recipe match timed out')
            content = json.dumps({'activate': True, 'objective': 'Produce a report',
                'task_family': 'report', 'reason': 'task'})
            return httpx.Response(200, json={'id': 'decision', 'object': 'chat.completion',
                'created': 0, 'model': 'model', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})
        return native_response(request)

    def algorithm(request):
        if request.url.path.endswith('/recipe-candidates'):
            return httpx.Response(200, json={'items': [{'recipe_candidate_id': 'candidate_1',
                'status': 'candidate', 'current': True}], 'has_more': False})
        if request.url.path.endswith('/recipe-candidates/candidate_1'):
            return httpx.Response(200, json={'recipe_candidate_id': 'candidate_1',
                'recipe_id': 'recipe_1', 'status': 'candidate', 'current': True,
                'version': 1, 'recipe': {'title': 'Report', 'when': 'Reports',
                'when_not': '', 'steps': []}})
        return _unexpected_algorithm(request)

    native = OpenAI(base_url='https://llm.cosens.ai/v1', api_key='test-only-gateway-key',
        http_client=httpx.Client(transport=httpx.MockTransport(gateway)))
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key',
        state_path=tmp_path/'match.sqlite', _http_transport=httpx.MockTransport(algorithm))
    try:
        response = wrapped.responses.create(model='model', input='Produce a report')
        assert response.output_text == 'final artifact'
        assert wrapped.cosens.rsi_capture == 'policy_failed'
        turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['state'] == 'completed' and not turn.get('execution_id')
    finally:
        wrapped.close(); native.close()


def test_auto_decision_timeout_returns_native_response(tmp_path):
    native = OpenAI(base_url='https://llm.cosens.ai/v1', api_key='test-only-gateway-key',
        http_client=httpx.Client(transport=httpx.MockTransport(_gateway)))
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key',
        state_path=tmp_path/'sync.sqlite', _http_transport=httpx.MockTransport(_unexpected_algorithm))
    try:
        wrapped.cosens.last_turn_id = 'previous-turn'
        response = wrapped.responses.create(model='model', input='Produce a report')
        assert response.output_text == 'final artifact'
        assert wrapped.cosens.rsi_capture == 'decision_failed'
        assert wrapped.cosens.last_turn_id is None
    finally:
        wrapped.close(); native.close()


@pytest.mark.asyncio
async def test_async_auto_decision_timeout_returns_native_response(tmp_path):
    native = AsyncOpenAI(base_url='https://llm.cosens.ai/v1', api_key='test-only-gateway-key',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(_gateway)))
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key',
        state_path=tmp_path/'async.sqlite', _http_transport=httpx.MockTransport(_unexpected_algorithm))
    try:
        response = await wrapped.responses.create(model='model', input='Produce a report')
        assert response.output_text == 'final artifact'
        assert wrapped.cosens.rsi_capture == 'decision_failed'
        assert wrapped.cosens.last_turn_id is None
    finally:
        await wrapped.close(); await native.close()
