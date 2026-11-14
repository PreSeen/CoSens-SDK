"""Request IDs on success and typed learning failures."""
import json

import httpx
import pytest

from cosens import CoSensClient, RSILearningFailed, RSILearningStopped

DETAIL = {'code': 'insufficient_credits', 'message': 'The model gateway reported insufficient credits.',
          'retryable': False, 'phase': 'analyzing', 'upstream_status': 402, 'upstream_code': 'gateway_credit_insufficient'}


def _client(handler):
    return CoSensClient(api_key='service-key', _http_transport=httpx.MockTransport(handler))


def test_successful_responses_carry_request_id_without_changing_the_value():
    def handler(request):
        if request.url.path.endswith('/tasks/task_1'):
            return httpx.Response(200, json={'task_id': 'task_1', 'request_id': 'from-body'}, headers={'x-request-id': 'hdr'})
        return httpx.Response(200, json={'task_id': 'task_2'}, headers={'x-request-id': 'req-123'})
    client = _client(handler)
    try:
        task = client.sync_atoms.get_task('task_2')
        assert task == {'task_id': 'task_2'} and task.request_id == 'req-123'
        assert json.dumps(task) == '{"task_id": "task_2"}'
        assert client.sync_atoms.get_task('task_1').request_id == 'from-body'
    finally:
        client.close()


def test_missing_request_id_header_is_none():
    client = _client(lambda request: httpx.Response(200, json={'task_id': 't'}))
    try:
        assert client.sync_atoms.get_task('t').request_id is None
    finally:
        client.close()


@pytest.mark.parametrize('payload,expected', [
    ({'error': 'insufficient_credits', 'error_detail': DETAIL},
     {'code': 'insufficient_credits', 'retryable': False, 'phase': 'analyzing', 'upstream_status': 402,
      'upstream_code': 'gateway_credit_insufficient'}),
    ({'error': 'ValueError', 'error_detail': None, 'phase': 'generating'},
     {'code': 'ValueError', 'retryable': False, 'phase': 'generating', 'upstream_status': None, 'upstream_code': None}),
])
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_wait_raises_typed_failure(payload, expected, asynchronous):
    body = {'execution_id': 'execution_1', 'learning': {'execution_id': 'execution_1', 'status': 'failed', **payload}}
    client = _client(lambda request: httpx.Response(200, json=body, headers={'x-request-id': 'req-9'}))
    try:
        with pytest.raises(RSILearningFailed) as caught:
            if asynchronous:
                await client.atoms.wait_learning('execution_1', timeout=1)
            else:
                client.sync_atoms.wait_learning('execution_1', timeout=1)
        error = caught.value
        assert error.execution_id == 'execution_1' and error.request_id == 'req-9'
        assert {key: getattr(error, key) for key in expected} == expected
    finally:
        await client.aclose()


def test_wait_stops_on_interrupted_with_the_learning():
    learning = {'execution_id': 'e', 'status': 'interrupted'}
    client = _client(lambda request: httpx.Response(200, json={'execution_id': 'e', 'learning': learning}))
    try:
        with pytest.raises(RSILearningStopped) as caught:
            client.sync_atoms.wait_learning('e', timeout=1)
        assert caught.value.status == 'interrupted' and caught.value.learning == learning
    finally:
        client.close()


def test_wait_learning_raises_and_keeps_the_failed_turn_resumable(tmp_path):
    from openai import OpenAI
    from cosens import cosens
    from test_explicit_recipe_protocol import Backend, native_response

    class FailedBackend(Backend):
        def __call__(self, request):
            if request.url.path.endswith('/recipe-candidates/candidate_1'):
                return httpx.Response(200, json={'recipe_candidate_id': 'candidate_1', 'status': 'failed',
                                                 'error': 'upstream_unavailable',
                                                 'error_detail': dict(DETAIL, code='upstream_unavailable', retryable=True)})
            return super().__call__(request)

    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(FailedBackend()))
    try:
        wrapped.responses.create(model='model', input='Produce a report')
        wrapped.cosens.generate_recipe()
        with pytest.raises(RSILearningFailed) as caught:
            wrapped.cosens.wait_learning(timeout=1)
        assert caught.value.code == 'upstream_unavailable' and caught.value.retryable is True
        assert wrapped.cosens.get_learning()['state'] == 'failed'
    finally:
        wrapped.close(); native.close()
