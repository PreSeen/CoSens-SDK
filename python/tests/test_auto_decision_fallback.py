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


def assert_failed_decision_turn(wrapped, previous):
    turn_id = wrapped.cosens.last_turn_id
    assert turn_id and turn_id != previous
    turn = wrapped.cosens.get_turn(turn_id)
    assert turn['state'] == 'completed' and turn['capture'] == 'decision_failed'
    assert turn['decision']['reason'] == 'decision_failed' and turn['decision']['activate'] is False
    assert turn['decision']['error_type'] == 'APITimeoutError'
    assert not turn.get('execution_id') and not turn.get('frozen_context')
    auto = wrapped.cosens.get_auto(turn_id)
    assert auto['capture'] == 'decision_failed' and auto['decision_error_type'] == 'APITimeoutError'


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


def test_auto_recipe_match_timeout_still_captures_without_a_recipe(tmp_path):
    """Reusing a Recipe is optional: a match timeout must not discard the capture."""
    from test_auto_latency import Algorithm, _candidate

    def gateway(request):
        if request.url.path.endswith('/chat/completions'):
            import json
            body = json.loads(request.content)
            name = body['response_format']['json_schema']['name']
            if name == 'cosens_recipe_match':
                raise httpx.ReadTimeout('recipe match timed out')
            content = json.dumps({'activate': True, 'reason': 'task'})
            return httpx.Response(200, json={'id': 'decision', 'object': 'chat.completion',
                'created': 0, 'model': 'model', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': content}, 'finish_reason': 'stop'}]})
        return native_response(request)

    algorithm = Algorithm([_candidate()])
    native = OpenAI(base_url='https://llm.cosens.ai/v1', api_key='test-only-gateway-key',
        http_client=httpx.Client(transport=httpx.MockTransport(gateway)))
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key',
        state_path=tmp_path/'match.sqlite', _http_transport=httpx.MockTransport(algorithm))
    try:
        response = wrapped.responses.create(model='model', input='Produce a report')
        assert response.output_text == 'final artifact'
        assert wrapped.cosens.rsi_capture == 'active'
        turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['state'] == 'completed' and turn['revision_id'] == 'revision_1'
        assert not turn.get('used_recipe_id')
        assert turn['recipe_match_error']['type'] == 'APITimeoutError'
        assert wrapped.cosens.get_auto()['recipe_match_error_type'] == 'APITimeoutError'
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
        # The fallback is recorded on a new turn so callers can see why nothing was captured.
        assert_failed_decision_turn(wrapped, 'previous-turn')
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
        # The fallback is recorded on a new turn so callers can see why nothing was captured.
        assert_failed_decision_turn(wrapped, 'previous-turn')
    finally:
        await wrapped.close(); await native.close()


@pytest.mark.parametrize('asynchronous', [False, True])
async def test_truncated_reasoning_decision_is_recorded_as_failed(tmp_path, asynchronous):
    """A reasoning model that spends max tokens before the JSON (finish_reason=length)."""
    def gateway(request):
        if request.url.path.endswith('/chat/completions'):
            return httpx.Response(200, json={'id': 'd', 'object': 'chat.completion', 'created': 0, 'model': 'model',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'length'}]})
        return native_response(request)

    if asynchronous:
        native = AsyncOpenAI(api_key='k', http_client=httpx.AsyncClient(transport=httpx.MockTransport(gateway)))
    else:
        native = OpenAI(api_key='k', http_client=httpx.Client(transport=httpx.MockTransport(gateway)))
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key', state_path=tmp_path/'t.sqlite',
        _http_transport=httpx.MockTransport(_unexpected_algorithm))
    try:
        call = wrapped.responses.create(model='model', input='What is the capital of France? One word.')
        response = await call if asynchronous else call
        assert response.output_text == 'final artifact'
        turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert turn['capture'] == 'decision_failed'
        assert turn['decision']['error_type'] == 'RSIDecisionUnavailable'
        assert 'finish_reason=length' in turn['decision']['error'] and 'max_output_tokens=1024' in turn['decision']['error']
    finally:
        if asynchronous:
            await wrapped.close(); await native.close()
        else:
            wrapped.close(); native.close()


def test_get_turn_without_an_id(tmp_path):
    native = OpenAI(api_key='k')
    wrapped = cosens(native, mode='auto', api_key='test-only-gateway-key', state_path=tmp_path/'t.sqlite')
    try:
        assert wrapped.cosens.last_turn_id is None
        assert wrapped.cosens.get_turn(None) is None
        with pytest.raises(ValueError, match='turn_id'):
            wrapped.cosens.get_turn(123)
    finally:
        wrapped.close(); native.close()
