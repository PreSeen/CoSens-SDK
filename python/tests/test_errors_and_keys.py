"""Request IDs on success and typed learning failures."""
import json

import httpx
import pytest

from cosens import (CoSensClient, RSIError, RSIFeedbackRequired, RSIHTTPError, RSILearningFailed, RSILearningStopped,
                    RSIRecoveryRequired)

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
     {'code': 'ValueError', 'retryable': None, 'phase': 'generating', 'upstream_status': None, 'upstream_code': None}),
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


def _wrapped_with_learning(tmp_path, terminal):
    """A transparent client whose first learning ends in ``terminal`` (a learning view update)."""
    from openai import OpenAI
    from cosens import cosens
    from test_explicit_recipe_protocol import Backend, native_response

    class TerminalBackend(Backend):
        def __call__(self, request):
            path = request.url.path.removeprefix('/api')
            if request.method == 'GET' and path == '/executions/execution_1' and self.resumed == 0:
                self.learnings['execution_1'].update(terminal)
            if path.endswith('/resume'):
                self.resumed += 1
            return super().__call__(request)

    backend = TerminalBackend()
    backend.resumed = 0
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(native_response)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(backend))
    wrapped.responses.create(model='model', input='Produce a report')
    wrapped.cosens.generate_recipe()
    return wrapped, native, backend


def test_wait_learning_raises_and_keeps_the_failed_turn_resumable(tmp_path):
    wrapped, native, backend = _wrapped_with_learning(tmp_path, {
        'status': 'failed', 'error': 'upstream_unavailable',
        'error_detail': dict(DETAIL, code='upstream_unavailable', retryable=True)})
    try:
        with pytest.raises(RSILearningFailed) as caught:
            wrapped.cosens.wait_learning(timeout=1)
        assert caught.value.code == 'upstream_unavailable' and caught.value.retryable is True
        assert caught.value.execution_id == 'execution_1'
        assert wrapped.cosens.get_learning()['state'] == 'failed'
        assert wrapped.cosens.resume_learning()['state'] == 'queued'
        assert wrapped.cosens.wait_learning(timeout=1)['state'] == 'completed'
    finally:
        wrapped.close(); native.close()


@pytest.mark.parametrize('status', ['interrupted', 'awaiting_runner'])
def test_wait_learning_surfaces_a_stopped_learning_and_keeps_the_turn_resumable(tmp_path, status):
    wrapped, native, backend = _wrapped_with_learning(tmp_path, {'status': status})
    try:
        with pytest.raises(RSILearningStopped) as caught:
            wrapped.cosens.wait_learning(timeout=1)
        assert caught.value.status == status and caught.value.execution_id == 'execution_1'
        assert wrapped.cosens.get_learning()['state'] == status
        assert wrapped.cosens.get_auto()['status'] == status
        assert wrapped.cosens.resume_learning()['state'] == 'queued'
        assert backend.resumed == 1
        assert wrapped.cosens.wait_learning(timeout=1)['state'] == 'completed'
    finally:
        wrapped.close(); native.close()


def test_wait_learning_surfaces_a_kept_version_without_resuming(tmp_path):
    wrapped, native, backend = _wrapped_with_learning(tmp_path, {'status': 'awaiting_material',
                                                                 'material_gap': 'no_actionable_feedback'})
    try:
        with pytest.raises(RSILearningStopped, match='no_actionable_feedback') as caught:
            wrapped.cosens.wait_learning(timeout=1)
        assert caught.value.status == 'awaiting_material'
        assert wrapped.cosens.get_learning()['state'] == 'awaiting_material'
        assert wrapped.cosens.resume_learning()['state'] == 'awaiting_material' and backend.resumed == 0
    finally:
        wrapped.close(); native.close()


def test_wait_learning_rejects_an_unknown_status(tmp_path):
    wrapped, native, backend = _wrapped_with_learning(tmp_path, {'status': 'paused'})
    try:
        with pytest.raises(RSIRecoveryRequired, match='paused'):
            wrapped.cosens.wait_learning(timeout=1)
    finally:
        wrapped.close(); native.close()


def test_wait_learning_on_a_request_without_a_learning_needs_recovery(tmp_path):
    wrapped, native, backend = _wrapped_with_learning(tmp_path, {})
    try:
        coordinator = wrapped.cosens._runtime.learning()
        turn_id = wrapped.cosens.last_turn_id
        record = coordinator.get(turn_id)
        record.pop('learning'); record['state'] = 'request_pending'
        coordinator.store.put(coordinator._key(turn_id), record)
        with pytest.raises(RSIRecoveryRequired, match='not confirmed'):
            wrapped.cosens.wait_learning(timeout=1)
        assert wrapped.cosens.resume_learning()['learning']['execution_id'] == 'execution_1'
    finally:
        wrapped.close(); native.close()


FEEDBACK_REQUIRED = {'error': {'code': 'feedback_required',
                               'message': 'learning needs feedback on this execution or an earlier draft; '
                                          'record it with POST /executions/{id}/feedback',
                               'allowed_sources': ['human', 'evaluator', 'reference', 'automated_check', 'downstream']}}


async def test_learn_without_feedback_raises_feedback_required_for_both_clients():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(422, json=FEEDBACK_REQUIRED, headers={'x-request-id': 'req-422'})
    async with _client(handler) as client:
        with pytest.raises(RSIFeedbackRequired) as caught:
            await client.atoms.learn('execution_1')
        with pytest.raises(RSIFeedbackRequired) as caught_sync:
            client.sync_atoms.learn('execution_1')
    for error in (caught.value, caught_sync.value):
        assert (error.status_code, error.code, error.request_id) == (422, 'feedback_required', 'req-422')
        assert error.allowed_sources == ['human', 'evaluator', 'reference', 'automated_check', 'downstream']
        assert 'POST /executions/{id}/feedback' in str(error)
        assert isinstance(error, RSIError) and not isinstance(error, RSIHTTPError)
    assert seen == [('POST', '/api/executions/execution_1/learn')] * 2


async def test_another_422_stays_an_http_error():
    payload = {'error': {'code': 'validation_error', 'message': 'bad', 'allowed_sources': ['human']}}
    async with _client(lambda request: httpx.Response(422, json=payload)) as client:
        with pytest.raises(RSIHTTPError) as caught:
            await client.atoms.learn('execution_1')
    assert caught.value.code == 'validation_error'
