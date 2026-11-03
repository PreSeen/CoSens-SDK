"""Unified auto: real SDK orchestration, model and HTTP boundaries replaced."""
import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from openai import OpenAI, AsyncOpenAI

from cosens import AutoLearning, cosens
from cosens.auto_learning import LearningCoordinator
from cosens.auto_policy import normalize


class Service:
    def __init__(self, matched=False):
        self.calls = []
        self.matched = matched
        self.fail_recipe = False
        self.separated_submission = True
        self.gate = None
        self.recipe_entered = threading.Event()
        self.head = 'old'
        self.portfolio = {'candidate_sha256': 'hash', 'diagnostic_candidates': [
            {'candidate_id': 'unsupported'}, {'candidate_id': 'cause', 'evidence_refs': ['event-1']}]}

    def recipe(self, candidate):
        return {'status': 'candidate', 'recipe_candidate_id': candidate, 'recipe_id': 'chain',
                'version': 1 if candidate == 'old' else 2, 'current': True,
                'recipe': {'title': 'Review', 'when': 'records exist', 'when_not': 'no records',
                           'steps': [{'instruction': 'Check the records'}]}}

    def __call__(self, request):
        path = request.url.path.removeprefix('/api')
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if path == '/v1/recipe-candidates':
            data = {'items': [self.recipe('old')] if self.matched else [], 'has_more': False}
        elif path.endswith('/memory/recall'):
            data = {'items': []}
        elif path == '/v1/artifacts':
            data = {'artifact_id': 'artifact-' + str(len(self.calls))}
        elif path == '/v1/tasks':
            data = {'task_id': 'task', 'status': 'ready'}
        elif path.endswith('/executions'):
            data = {'execution_id': 'execution', 'request_sha256': 'request-hash', 'status': 'awaiting_execution',
                    'request': {'method': self.recipe('old')} if self.matched else {}}
            if self.matched:
                assert body['recipe_id'] == 'chain'
        elif path.endswith('/result'):
            assert 'generate_recipe' not in body
            if not self.separated_submission:
                return httpx.Response(200, json={'status': 'completed', 'revision_id': 'revision',
                                                  'initial_recipe_candidate_id': 'unexpected-auto-v1'})
            data = {'status': 'completed', 'revision_id': 'revision'}
        elif path.endswith('/improvements'):
            data = {'attribution_id': 'attribution'}
        elif path == '/v1/attributions/attribution':
            data = {'status': 'awaiting_selection', 'portfolio': self.portfolio}
        elif path.endswith('/selection'):
            assert body['data']['candidate_id'] == 'cause'
            data = {'recipe_candidate_id': 'v2'}
        elif path.endswith('/recipe-candidates'):
            data = {'recipe_candidate_id': 'v1'}
        elif path == '/v1/tasks/task/recipe':
            data = {'current': self.recipe(self.head)}
        elif path.endswith('/resume'):
            self.fail_recipe = False
            data = {'status': 'queued'}
        elif '/recipe-candidates/' in path:
            candidate = path.rsplit('/', 1)[-1]
            if self.gate and candidate != 'old':
                self.recipe_entered.set()
                assert self.gate.wait(3)
            data = self.recipe(candidate)
            if self.fail_recipe:
                data['status'] = 'failed'
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=data)


class Model:
    def __init__(self, *, action='create', sufficient=True, feedback=True, activate=True, tool=False):
        self.action, self.sufficient = action, sufficient
        self.feedback, self.activate, self.tool = feedback, activate, tool
        self.calls, self.responses = [], []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs.get('text', {}).get('format', {}).get('name')
        if name == 'cosens_decision':
            contextual = 'feedback' in kwargs['text']['format']['schema']['properties']
            data = {'activate': self.activate and not contextual, 'objective': 'task', 'task_family': 'general', 'reason': 'test'}
            if contextual:
                data['feedback'] = self.feedback
        elif name == 'cosens_recipe_match':
            data = {'candidate_id': 'old', 'reason': 'applicable'}
        elif name == 'cosens_auto_policy':
            data = {'action': 'revise' if '"feedback": "Fix format"' in kwargs['input'] else self.action,
                    'context_sufficient': self.sufficient, 'reason': 'evidence checked'}
        else:
            response = SimpleNamespace(id='resp-' + str(len(self.responses)), status='completed', output_text='answer',
                output=[SimpleNamespace(type='message', role='assistant')])
            if self.tool and not self.responses:
                response.output_text = ''
                response.output = [SimpleNamespace(type='function_call', call_id='c1', name='read', arguments='{}')]
            self.responses.append(response)
            return response
        return SimpleNamespace(output_text=json.dumps(data))

    async def acreate(self, **kwargs):
        return self.create(**kwargs)


def wrap(tmp_path, service, model, async_mode=False, **options):
    native = AsyncOpenAI(api_key='native-secret') if async_mode else OpenAI(api_key='native-secret')
    native.responses.create = model.acreate if async_mode else model.create
    native.chat.completions.create = model.acreate if async_mode else model.create
    return cosens(native, mode='auto', decision_model='decision', api_key='test',
        auto_learning=AutoLearning(poll_interval=.01, **options), state_path=tmp_path / 'state.db',
        _http_transport=httpx.MockTransport(service))


def close(client):
    worker = client._runtime._learning
    client.close()
    if worker and worker.thread:
        worker.thread.join(3)
        assert not worker.thread.is_alive()


@pytest.mark.parametrize('matched,action,sufficient,expected', [
    (False, 'create', True, 'create'), (False, 'create', False, 'noop'),
    (False, 'noop', True, 'noop'), (True, 'create', True, 'noop')])
def test_three_outcomes_and_reuse(tmp_path, matched, action, sufficient, expected):
    service, model = Service(matched), Model(action=action, sufficient=sufficient)
    client = wrap(tmp_path, service, model)
    try:
        response = client.responses.create(model='app', input='task')
        assert response is model.responses[0]
        record = client.cosens.wait_learning(timeout=3)
        assert record['action'] == expected
        assert record['state'] == ('noop' if expected == 'noop' else 'completed')
        assert client.cosens.get_auto()['action'] == expected
        assert len(model.responses) == 1
        mutations = [p for method, p, _ in service.calls if method == 'POST' and p.endswith('/recipe-candidates')]
        assert len(mutations) == (expected == 'create')
        assert not any('/runs/' in p for _, p, _ in service.calls)
        if matched:
            assert record['used_recipe_id'] == 'chain'
            assert record['used_recipe_version'] == 1
            assert 'Check the records' in model.calls[-1]['instructions']
            assert not any(c.get('text', {}).get('format', {}).get('name') == 'cosens_auto_policy' for c in model.calls)
    finally:
        close(client)


@pytest.mark.asyncio
@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('link', ['response', 'chat'])
async def test_native_feedback_revises_the_linked_version(tmp_path, async_mode, link):
    service, model = Service(True), Model()
    client = wrap(tmp_path, service, model, async_mode)
    try:
        method = client.responses.create if link == 'response' else client.chat.completions.create
        first_kwargs = {'model': 'app', 'input': 'task'} if link == 'response' else {
            'model': 'app', 'messages': [{'role': 'user', 'content': 'task'}]}
        first = await method(**first_kwargs) if async_mode else method(**first_kwargs)
        turn = client.cosens.last_turn_id
        initial = await client.cosens.wait_learning(turn, timeout=3) if async_mode else client.cosens.wait_learning(turn, timeout=3)
        assert initial['action'] == 'noop'
        second_kwargs = {'model': 'app', 'previous_response_id': first.id, 'input': 'Fix format'} if link == 'response' else {
            'model': 'app', 'messages': [{'role': 'user', 'content': 'task'}, {'role': 'assistant', 'content': 'answer'},
                                       {'role': 'user', 'content': 'Fix format'}]}
        second = await method(**second_kwargs) if async_mode else method(**second_kwargs)
        assert second is model.responses[1]
        record = await client.cosens.wait_learning(turn, timeout=3) if async_mode else client.cosens.wait_learning(turn, timeout=3)
        assert record['action'] == 'revise' and record['state'] == 'completed'
        assert record['recipe_candidate_id'] == 'v2'
        assert record['used_recipe_candidate_id'] == 'old'
        assert record['human_confirmed'] is False
        assert client.cosens.get_auto()['action'] == 'revise'
        assert not any(p.endswith('/recipe-candidates') for method, p, _ in service.calls if method == 'POST')
        assert sum(p.endswith('/improvements') for _, p, _ in service.calls) == 1
        assert client._runtime._feedback_target(('responses', 'create'), {'input': 'unrelated'}) is None
    finally:
        worker = client._runtime._learning
        if async_mode:
            await client.close()
            if worker and worker.thread:
                await asyncio.to_thread(worker.thread.join, 3)
        else:
            close(client)


def test_recipe_generation_does_not_block_business_response(tmp_path):
    service, model = Service(), Model()
    service.gate = threading.Event()
    client = wrap(tmp_path, service, model)
    try:
        response = client.responses.create(model='app', input='task')
        assert response is model.responses[0]
        assert client.cosens.get_turn(client.cosens.last_turn_id)['state'] == 'completed'
        with pytest.raises(TimeoutError):
            client.cosens.wait_learning(timeout=.05)
        service.gate.set()
        assert client.cosens.wait_learning(timeout=3)['state'] == 'completed'
    finally:
        service.gate.set()
        close(client)


def test_tool_continuation_freezes_lookup_and_captures_actual_trace(tmp_path):
    service, model = Service(), Model(tool=True)
    client = wrap(tmp_path, service, model)
    try:
        first = client.responses.create(model='app', input='task')
        turn = client.cosens.last_turn_id
        assert client.cosens.get_learning() is None
        client.responses.create(model='app', previous_response_id=first.id,
            input=[{'type': 'function_call_output', 'call_id': 'c1', 'output': 'checked'}])
        assert client.cosens.last_turn_id == turn
        assert client.cosens.wait_learning(timeout=3)['action'] == 'create'
        assert sum(p == '/v1/recipe-candidates' and method == 'GET' for method, p, _ in service.calls) == 1
        submitted = next(b for _, p, b in service.calls if p.endswith('/result'))
        assert any(e['kind'] == 'tool_result' and e['payload']['content'] == 'checked' for e in submitted['events'])
        assert sum(p.endswith('/result') for _, p, _ in service.calls) == 1
    finally:
        close(client)


def test_old_service_does_not_report_fake_noop_success(tmp_path):
    service, model = Service(), Model()
    service.separated_submission = False
    client = wrap(tmp_path, service, model)
    try:
        assert client.responses.create(model='app', input='task') is model.responses[0]
        auto = client.cosens.get_auto()
        assert auto['status'] == 'blocked' and auto['reason'] == 'completion_pending'
        assert not any(p.endswith('/recipe-candidates') for method, p, _ in service.calls if method == 'POST')
        service.separated_submission = True
        client.cosens.reconcile(client.cosens.last_turn_id)
        assert client.cosens.wait_learning(timeout=3)['action'] == 'create'
        assert len(model.responses) == 1
    finally:
        close(client)


def test_small_talk_is_noop_and_does_not_create_task(tmp_path):
    service, model = Service(), Model(activate=False)
    client = wrap(tmp_path, service, model)
    try:
        client.responses.create(model='app', input='hello')
        assert client.cosens.wait_learning(timeout=3)['action'] == 'noop'
        assert not any(p == '/v1/tasks' for _, p, _ in service.calls)
        assert any(p == '/v1/recipe-candidates' for _, p, _ in service.calls)
    finally:
        close(client)


def test_same_remote_job_retry_and_credentials_not_persisted(tmp_path):
    service, model = Service(), Model()
    service.fail_recipe = True
    client = wrap(tmp_path, service, model,
        model_api={'base_url': 'https://model.example/v1', 'model': 'model', 'api_key': 'private-model-secret'})
    try:
        client.responses.create(model='app', input='task')
        record = client.cosens.wait_learning(timeout=3)
        assert record['state'] == 'completed'
        assert any(p == '/v1/recipe-candidates/v1/resume' for _, p, _ in service.calls)
        assert 'private-model-secret' not in json.dumps(record)
    finally:
        close(client)
    assert b'private-model-secret' not in (tmp_path / 'state.db').read_bytes()


@pytest.mark.parametrize('action,has_recipe,feedback,recorded,expected', [
    ('create', True, None, True, 'noop'), ('revise', False, 'bad', True, 'noop'),
    ('create', False, None, False, 'noop'), ('revise', True, 'bad', True, 'revise')])
def test_policy_invariants(action, has_recipe, feedback, recorded, expected):
    assert normalize({'action': action, 'reason': 'test', 'context_sufficient': True},
        has_recipe=has_recipe, feedback=feedback, recorded=recorded)['action'] == expected


def test_feedback_during_creation_is_processed_after_parent_is_ready(tmp_path):
    service, model = Service(), Model()
    service.gate = threading.Event()
    service.head = 'v1'
    client = wrap(tmp_path, service, model)
    try:
        first = client.responses.create(model='app', input='task')
        turn = client.cosens.last_turn_id
        assert service.recipe_entered.wait(3)
        client.responses.create(model='app', previous_response_id=first.id, input='Fix format')
        service.gate.set()
        record = client.cosens.wait_learning(turn, timeout=3)
        assert record['state'] == 'completed' and record['action'] == 'revise'
        assert sum(p.endswith('/improvements') for _, p, _ in service.calls) == 1
    finally:
        service.gate.set()
        close(client)


def test_duplicate_context_does_not_launch_another_creation(tmp_path):
    service, model = Service(), Model()
    client = wrap(tmp_path, service, model)
    try:
        client.responses.create(model='app', input='task')
        assert client.cosens.wait_learning(timeout=3)['action'] == 'create'
        # The fake search intentionally omits the candidate just generated.
        client.responses.create(model='app', input='task')
        record = client.cosens.wait_learning(timeout=3)
        assert record['action'] == 'noop'
        assert record['reason'] == 'recipe_creation_already_requested'
        assert sum(p.endswith('/recipe-candidates') for method, p, _ in service.calls if method == 'POST') == 1
    finally:
        close(client)


def test_recovery_does_not_repeat_business_execution(tmp_path):
    service, model = Service(), Model()
    service.fail_recipe = True
    client = wrap(tmp_path, service, model, max_job_retries=0)
    client.responses.create(model='app', input='task')
    turn = client.cosens.last_turn_id
    assert client.cosens.wait_learning(timeout=3)['state'] == 'failed'
    close(client)
    service.fail_recipe = False
    recovered = wrap(tmp_path, service, model)
    try:
        recovered.cosens.resume_learning(turn)
        assert recovered.cosens.wait_learning(turn, timeout=3)['state'] == 'completed'
        assert len(model.responses) == 1
        assert sum(p.endswith('/recipe-candidates') for method, p, _ in service.calls if method == 'POST') == 1
    finally:
        close(recovered)


def test_parent_changed_does_not_silently_revise_a_different_version(tmp_path):
    service, model = Service(True), Model()
    client = wrap(tmp_path, service, model)
    try:
        first = client.responses.create(model='app', input='task')
        turn = client.cosens.last_turn_id
        client.cosens.wait_learning(turn, timeout=3)
        service.head = 'newer'
        client.responses.create(model='app', previous_response_id=first.id, input='Fix format')
        record = client.cosens.wait_learning(turn, timeout=3)
        assert record['state'] == 'failed'
        assert not any(p.endswith('/improvements') for _, p, _ in service.calls)
    finally:
        close(client)


@pytest.mark.parametrize('matched', [False, True])
def test_incomplete_search_only_blocks_creation_not_known_recipe_feedback(tmp_path, matched):
    service, model = Service(matched), Model()
    def server(request):
        response = service(request)
        if request.method == 'GET' and request.url.path.endswith('/recipe-candidates'):
            data = response.json()
            data['has_more'] = True
            return httpx.Response(200, json=data)
        return response
    client = wrap(tmp_path, server, model)
    try:
        first = client.responses.create(model='app', input='task')
        turn = client.cosens.last_turn_id
        initial = client.cosens.wait_learning(turn, timeout=3)
        assert initial['action'] == 'noop'
        if matched:
            assert initial['reason'] == 'reuse_without_feedback'
            client.responses.create(model='app', previous_response_id=first.id, input='Fix format')
            result = client.cosens.wait_learning(turn, timeout=3)
            assert result['action'] == 'revise' and result['state'] == 'completed'
        else:
            assert initial['reason'] == 'recipe_search_incomplete'
            assert not any(p.endswith('/recipe-candidates') for method, p, _ in service.calls if method == 'POST')
    finally:
        close(client)
