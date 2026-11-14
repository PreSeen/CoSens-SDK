"""Atoms of the execution API: corrections, feedback and learning are keyed by execution."""
import inspect
import json

import httpx
import pytest

import cosens
from cosens import CoSensClient, RSIError, RSILearningFailed, RSILearningStopped

REMOVED = ('get_revision', 'start_revision', 'start_recipe_candidate', 'get_recipe_candidate',
           'resume_recipe_candidate', 'list_recipe_candidates', 'wait_workflow_job', 'recall_context',
           'start_session', 'append_session_events', 'get_session_context', 'end_session')
MODES = (False, True)
DETAIL = {'code': 'upstream_unavailable', 'message': 'The model gateway was unavailable.', 'retryable': True,
          'phase': 'analyzing', 'upstream_status': 503, 'upstream_code': 'gateway_unavailable'}


def _learning(status, **fields):
    return {'execution_id': 'e1', 'recipe_id': 'r1', 'version': 1, 'previous_version': None, 'current': False,
            'status': status, 'phase': 'queued', 'attribution': None, 'recipe': None, 'recipe_sha256': None,
            'error': None, 'error_detail': None, 'models': None, 'memory_context': 'captured', 'initial': True,
            'material_gap': None, 'max_completion_tokens': 200_000, **fields}


class Server:
    """Records (method, path, body, idempotency key); serves the execution view from ``views`` in order."""

    def __init__(self, views=()):
        self.seen = []
        self.views = list(views)

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, body, request.headers.get('idempotency-key')))
        if request.method == 'GET' and request.url.path == '/api/executions/e1':
            view = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return httpx.Response(200, json={'execution_id': 'e1', 'task_id': 't1', 'learning': view},
                                  headers={'x-request-id': 'req-9'})
        return httpx.Response(200, json={'execution_id': 'e1', 'status': 'queued'})


async def _call(atoms, name, *args, **kwargs):
    result = getattr(atoms, name)(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


async def _run(asynchronous, server, name, *args, **kwargs):
    async with CoSensClient(api_key='test-key', _http_transport=httpx.MockTransport(server)) as client:
        return await _call(client.atoms if asynchronous else client.sync_atoms, name, *args, **kwargs)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def asleep(_):
        return None
    monkeypatch.setattr('cosens.workflow_atoms.time.sleep', lambda _: None)
    monkeypatch.setattr('cosens.workflow_atoms.asyncio.sleep', asleep)


@pytest.mark.parametrize('asynchronous', MODES)
async def test_start_execution_sends_a_correction_with_previous_execution_id(asynchronous):
    server = Server()
    await _run(asynchronous, server, 'start_execution', 't1', key='k', previous_execution_id='e0')
    assert server.seen == [('POST', '/api/tasks/t1/executions', {'previous_execution_id': 'e0'}, 'k')]


@pytest.mark.parametrize('asynchronous', MODES)
@pytest.mark.parametrize('extra', [{'recipe_id': 'r'}, {'recall_memory': True}])
async def test_a_correction_rejects_recipe_and_memory_before_sending(asynchronous, extra):
    server = Server()
    with pytest.raises(ValueError, match='previous_execution_id'):
        await _run(asynchronous, server, 'start_execution', 't1', key='k', previous_execution_id='e0', **extra)
    assert server.seen == []


@pytest.mark.parametrize('asynchronous', MODES)
async def test_a_correction_may_name_the_model(asynchronous):
    server = Server()
    await _run(asynchronous, server, 'start_execution', 't1', key='k', previous_execution_id='e0', model='m')
    assert server.seen[0][2] == {'model': 'm', 'previous_execution_id': 'e0'}


@pytest.mark.parametrize('asynchronous', MODES)
async def test_record_feedback_is_keyed_by_execution(asynchronous):
    server = Server()
    await _run(asynchronous, server, 'record_feedback', 'e1', feedback='f', source='human', key='kf')
    assert server.seen == [('POST', '/api/executions/e1/feedback', {'feedback': 'f', 'source': 'human'}, 'kf')]


@pytest.mark.parametrize('asynchronous', MODES)
async def test_learn_and_resume_send_no_idempotency_key(asynchronous):
    server = Server()
    await _run(asynchronous, server, 'learn', 'e1', max_completion_tokens=200_000)
    await _run(asynchronous, server, 'learn', 'e1')
    await _run(asynchronous, server, 'resume_learning', 'e1', model='m')
    await _run(asynchronous, server, 'resume_learning', 'e1')
    assert server.seen == [
        ('POST', '/api/executions/e1/learn', {'max_completion_tokens': 200_000}, None),
        ('POST', '/api/executions/e1/learn', {}, None),
        ('POST', '/api/executions/e1/resume', {'model': 'm'}, None),
        ('POST', '/api/executions/e1/resume', None, None),
    ]


@pytest.mark.parametrize('asynchronous', MODES)
@pytest.mark.parametrize('name', ['learn', 'resume_learning'])
async def test_invalid_learning_budget_is_rejected_before_sending(asynchronous, name):
    server = Server()
    with pytest.raises(ValueError, match='max_completion_tokens'):
        await _run(asynchronous, server, name, 'e1', max_completion_tokens=0)
    assert server.seen == []


@pytest.mark.parametrize('asynchronous', MODES)
async def test_wait_learning_returns_the_completed_learning(asynchronous):
    server = Server([_learning('queued'), _learning('running'), _learning('completed', current=True)])
    learning = await _run(asynchronous, server, 'wait_learning', 'e1', timeout=60)
    assert learning['status'] == 'completed' and learning['current'] is True
    assert [entry[:2] for entry in server.seen] == [('GET', '/api/executions/e1')] * 3


@pytest.mark.parametrize('asynchronous', MODES)
async def test_wait_learning_raises_typed_failure_from_error_detail(asynchronous):
    server = Server([_learning('queued'), _learning('failed', error='upstream_unavailable', error_detail=DETAIL)])
    with pytest.raises(RSILearningFailed) as caught:
        await _run(asynchronous, server, 'wait_learning', 'e1', timeout=60)
    error = caught.value
    assert (error.execution_id, error.code, error.retryable, error.phase, error.upstream_status, error.upstream_code) == \
        ('e1', 'upstream_unavailable', True, 'analyzing', 503, 'gateway_unavailable')
    assert str(error) == 'Learning failed for execution e1: upstream_unavailable'
    assert not isinstance(error, RSILearningStopped)


def test_failure_without_error_detail_uses_error_and_is_not_retryable():
    error = RSILearningFailed.from_learning(_learning('failed', error='context_changed', phase='generating'))
    assert (error.execution_id, error.code, error.retryable, error.phase, error.upstream_status, error.upstream_code) == \
        ('e1', 'context_changed', False, 'generating', None, None)
    assert error.message == 'context_changed'


@pytest.mark.parametrize('asynchronous', MODES)
@pytest.mark.parametrize('status', ['interrupted', 'awaiting_runner', 'awaiting_material'])
async def test_wait_learning_stops_on_states_only_the_caller_can_change(asynchronous, status):
    view = _learning(status, material_gap='the original input' if status == 'awaiting_material' else None)
    server = Server([_learning('queued'), view])
    with pytest.raises(RSILearningStopped) as caught:
        await _run(asynchronous, server, 'wait_learning', 'e1', timeout=60)
    error = caught.value
    assert error.status == status and error.execution_id == 'e1' and error.learning == view
    assert not isinstance(error, RSILearningFailed) and isinstance(error, RSIError)
    assert len(server.seen) == 2


@pytest.mark.parametrize('asynchronous', MODES)
async def test_wait_learning_times_out_on_a_learning_still_in_flight(asynchronous):
    server = Server([_learning('awaiting_previous_version')])
    with pytest.raises(TimeoutError, match='e1'):
        await _run(asynchronous, server, 'wait_learning', 'e1', timeout=0)


@pytest.mark.parametrize('asynchronous', MODES)
async def test_wait_learning_without_a_learning_needs_learn_first(asynchronous):
    def server(request):
        return httpx.Response(200, json={'execution_id': 'e1', 'learning': None})
    with pytest.raises(cosens.RSIRecoveryRequired, match='learn'):
        await _run(asynchronous, server, 'wait_learning', 'e1', timeout=1)


async def test_removed_atoms_are_gone():
    async with CoSensClient(api_key='test-key', _http_transport=httpx.MockTransport(Server())) as client:
        for atoms in (client.atoms, client.sync_atoms):
            assert [name for name in REMOVED if hasattr(atoms, name)] == []
    from cosens import atoms_helpers, errors
    assert not hasattr(cosens, 'RSICandidateFailed') and not hasattr(errors, 'RSICandidateFailed')
    assert {'RSILearningFailed', 'RSILearningStopped'} <= set(cosens.__all__)
    assert not hasattr(atoms_helpers, 'recipe_request_key')
