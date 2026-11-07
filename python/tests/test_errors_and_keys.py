"""Request IDs on success, typed candidate failures and default generation keys."""
import json

import httpx
import pytest

from cosens import CoSensClient, RSICandidateFailed
from cosens.atoms_helpers import recipe_request_key

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
    ({'error': 'ValueError', 'phase': 'generating'},
     {'code': 'ValueError', 'retryable': None, 'phase': 'generating', 'upstream_status': None, 'upstream_code': None}),
])
@pytest.mark.parametrize('asynchronous', [False, True])
async def test_wait_raises_typed_failure(payload, expected, asynchronous):
    body = {'recipe_candidate_id': 'candidate_1', 'status': 'failed', **payload}
    client = _client(lambda request: httpx.Response(200, json=body, headers={'x-request-id': 'req-9'}))
    try:
        with pytest.raises(RSICandidateFailed) as caught:
            if asynchronous:
                await client.atoms.wait_workflow_job('recipe_candidate', 'candidate_1', timeout=1)
            else:
                client.sync_atoms.wait_workflow_job('recipe_candidate', 'candidate_1', timeout=1)
        error = caught.value
        assert error.candidate_id == 'candidate_1' and error.request_id == 'req-9'
        assert {key: getattr(error, key) for key in expected} == expected
    finally:
        await client.aclose()


def test_wait_returns_non_failed_terminal_states():
    client = _client(lambda request: httpx.Response(200, json={'recipe_candidate_id': 'c', 'status': 'interrupted'}))
    try:
        assert client.sync_atoms.wait_workflow_job('recipe_candidate', 'c', timeout=1)['status'] == 'interrupted'
    finally:
        client.close()


def test_default_recipe_key_is_deterministic_and_matches_typescript():
    # The same vectors are asserted in typescript/tests/errors-and-keys.test.ts.
    assert recipe_request_key('task_1', 'revision_1') == \
        'recipe-f1880ef284cfb24705367206c39a433e643c3907f6e2953c9a44491e3d9d8513'
    assert recipe_request_key('task_1', 'revision_1', 200_000) == \
        'recipe-8f22d4789600f0311e6526ff172a97e48da326ad7af0e9899afc2fad09bf0c75'
    keys = []

    def handler(request):
        keys.append(request.headers.get('Idempotency-Key'))
        return httpx.Response(200, json={'recipe_candidate_id': 'c', 'status': 'queued'})
    client = _client(handler)
    try:
        client.sync_atoms.start_recipe_candidate('task_1', revision_id='revision_1')
        client.sync_atoms.start_recipe_candidate('task_1', revision_id='revision_1')
        client.sync_atoms.start_recipe_candidate('task_1', revision_id='revision_1', max_completion_tokens=200_000)
        client.sync_atoms.start_recipe_candidate('task_1', revision_id='revision_1', key='explicit')
        assert keys == [recipe_request_key('task_1', 'revision_1')] * 2 + [
            recipe_request_key('task_1', 'revision_1', 200_000), 'explicit']
    finally:
        client.close()


def test_wait_learning_raises_and_keeps_the_failed_turn_resumable(tmp_path):
    from openai import OpenAI
    from cosens import cosens
    from test_explicit_recipe_protocol import Backend, native_response

    class FailedBackend(Backend):
        def __call__(self, request):
            if request.url.path.endswith('/v1/recipe-candidates/candidate_1'):
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
        with pytest.raises(RSICandidateFailed) as caught:
            wrapped.cosens.wait_learning(timeout=1)
        assert caught.value.code == 'upstream_unavailable' and caught.value.retryable is True
        assert wrapped.cosens.get_learning()['state'] == 'failed'
    finally:
        wrapped.close(); native.close()
