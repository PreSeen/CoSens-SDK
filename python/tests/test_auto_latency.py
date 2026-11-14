"""The auto policy call is short, unretried and skipped when no Recipe exists."""
import json

import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from test_explicit_recipe_protocol import native_response


def _chat(content):
    return httpx.Response(200, json={'id': 'decision', 'object': 'chat.completion', 'created': 0, 'model': 'model',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(content)}, 'finish_reason': 'stop'}]})


ACTIVATE = {'activate': True, 'reason': 'task'}
DECLINE = {'activate': False, 'reason': 'routine'}


class Algorithm:
    """Minimal service: optional Recipe candidates, Memory, task and execution."""

    def __init__(self, candidates=()):
        self.paths = []
        self.candidates = {c['recipe_candidate_id']: c for c in candidates}

    def __call__(self, request):
        path = request.url.path.removeprefix('/api')
        self.paths.append(path)
        if path == '/recipe-candidates':
            items = [{'recipe_candidate_id': key, 'status': 'candidate', 'current': True, 'title': c['recipe']['title']}
                     for key, c in self.candidates.items()]
            return httpx.Response(200, json={'items': items, 'has_more': False})
        if path.startswith('/recipe-candidates/'):
            return httpx.Response(200, json=self.candidates[path.rsplit('/', 1)[1]])
        if path == '/v1/memory/recall':
            return httpx.Response(200, json={'items': [{'id': 'm1', 'kind': 'source', 'text': 'Reports use the Q3 template.',
                                                        'refs': [{'source_id': 's1'}], 'score': 0.9}]})
        if path == '/artifacts':
            return httpx.Response(200, json={'artifact_id': 'artifact_1', 'content_hash': 'sha256:x'})
        if path == '/tasks':
            return httpx.Response(200, json={'task_id': 'task_1', 'status': 'ready'})
        if path.endswith('/executions'):
            body = json.loads(request.content)
            method = None
            if body.get('recipe_id'):
                chosen = next(c for c in self.candidates.values() if c['recipe_id'] == body['recipe_id'])
                method = {'recipe_id': chosen['recipe_id'], 'recipe_candidate_id': chosen['recipe_candidate_id'],
                          'version': chosen['version']}
            return httpx.Response(200, json={'execution_id': 'execution_1', 'status': 'awaiting_execution',
                                             'request_sha256': 'sha', 'request': {'method': method}})
        if path.endswith('/result'):
            return httpx.Response(200, json={'status': 'completed', 'revision_id': 'revision_1'})
        raise AssertionError(path)


def _candidate(candidate_id='candidate_1', steps=3):
    return {'recipe_candidate_id': candidate_id, 'recipe_id': 'recipe_1', 'status': 'candidate', 'current': True,
            'version': 1, 'recipe': {'title': 'Quarterly report', 'when': 'Writing a report', 'when_not': 'Chat',
            'steps': [{'id': f's{n}', 'instruction': f'Do step {n}', 'evidence_refs': [{'event_id': f'e{n}'}]}
                      for n in range(1, steps + 1)],
            'lessons': [{'when': 'Always', 'do': 'Cite the source', 'why': 'Trust', 'when_not': 'Never',
                         'evidence_refs': [{'event_id': 'e1'}]}]}}


def test_decision_defaults_cap_tokens_and_do_not_retry(tmp_path):
    calls = []

    def model(request):
        if request.url.path.endswith('/chat/completions'):
            calls.append(json.loads(request.content))
            return httpx.Response(500, json={'error': {'message': 'overloaded'}})
        return native_response(request)

    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(Algorithm()))
    try:
        assert wrapped.responses.create(model='model', input='Produce a report').output_text == 'final artifact'
        # One attempt only; the default client would have retried twice.
        assert len(calls) == 1
        assert calls[0]['max_completion_tokens'] == 1024
        assert wrapped.cosens.rsi_capture == 'decision_failed'
        assert native.max_retries == 2  # the caller's own client is unchanged
    finally:
        wrapped.close(); native.close()


def test_decision_client_and_extra_body_cover_decision_and_match(tmp_path):
    native_paths, decision_bodies = [], []

    def model(request):
        native_paths.append(request.url.path)
        return native_response(request)

    def decider(request):
        body = json.loads(request.content)
        decision_bodies.append(body)
        if body['response_format']['json_schema']['name'] == 'cosens_recipe_match':
            return _chat({'candidate_id': 'candidate_1'})
        return _chat(ACTIVATE)

    algorithm = Algorithm([_candidate()])
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    decision_client = OpenAI(api_key='decision-key', http_client=httpx.Client(transport=httpx.MockTransport(decider)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     decision_client=decision_client, decision_extra_body={'enable_thinking': False},
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        wrapped.responses.create(model='model', input='Produce a report')
        assert native_paths == ['/v1/responses']
        names = [body['response_format']['json_schema']['name'] for body in decision_bodies]
        assert names == ['cosens_decision', 'cosens_recipe_match']
        assert all(body['enable_thinking'] is False and body['max_completion_tokens'] == 1024 for body in decision_bodies)
        # The matcher sees applicability only, never the steps or evidence.
        match_input = decision_bodies[1]['messages'][0]['content']
        assert 'Writing a report' in match_input and 'Do step 1' not in match_input and 'evidence_refs' not in match_input
        assert wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)['used_recipe_id'] == 'recipe_1'
    finally:
        wrapped.close(); native.close(); decision_client.close()


def test_decision_client_must_match_the_wrapped_client(tmp_path):
    native = OpenAI(api_key='model-key')
    with pytest.raises(TypeError, match='AsyncOpenAI'):
        cosens(AsyncOpenAI(api_key='model-key'), decision_client=native, state_path=tmp_path / 's.sqlite')
    with pytest.raises(ValueError, match='max_context_chars'):
        cosens(native, max_context_chars=10, state_path=tmp_path / 's.sqlite')


def test_empty_recipe_list_skips_match_and_is_cached(tmp_path):
    decision_names = []

    def model(request):
        if request.url.path.endswith('/chat/completions'):
            decision_names.append(json.loads(request.content)['response_format']['json_schema']['name'])
        return native_response(request)

    algorithm = Algorithm()
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        wrapped.responses.create(model='model', input='Produce a report')
        wrapped.responses.create(model='model', input='Produce another report')
        assert decision_names == ['cosens_decision', 'cosens_decision']
        assert algorithm.paths.count('/recipe-candidates') == 1
        assert algorithm.paths.count('/v1/memory/recall') == 2
    finally:
        wrapped.close(); native.close()


def test_injected_context_is_compact_and_bounded(tmp_path):
    sent = []

    def model(request):
        body = json.loads(request.content)
        if request.url.path.endswith('/chat/completions'):
            name = body['response_format']['json_schema']['name']
            return _chat({'candidate_id': 'candidate_1'} if name == 'cosens_recipe_match' else ACTIVATE)
        sent.append(body)
        return native_response(request)

    algorithm = Algorithm([_candidate(steps=30)])
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     max_context_chars=400, _http_transport=httpx.MockTransport(algorithm))
    try:
        wrapped.responses.create(model='model', input='Produce a report', instructions='Be brief.')
        instructions = sent[0]['instructions']
        assert instructions.startswith('Be brief.\n\n[CoSens context:')
        block = instructions.removeprefix('Be brief.\n\n')
        assert len(block) <= 400
        assert 'Recipe (provisional): Quarterly report' in block and '1. Do step 1' in block
        assert 'evidence_refs' not in block and 'score' not in block
        assert block.splitlines()[-1].startswith('[CoSens context truncated to 400 characters;')
    finally:
        wrapped.close(); native.close()


def test_declined_turn_makes_no_lookup(tmp_path):
    def model(request):
        if request.url.path.endswith('/chat/completions'):
            return _chat(DECLINE)
        return native_response(request)

    algorithm = Algorithm([_candidate()])
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        wrapped.responses.create(model='model', input='Say hi')
        assert algorithm.paths == [] and wrapped.cosens.rsi_capture == 'skipped'
    finally:
        wrapped.close(); native.close()


# Largest completion (reasoning + JSON) of a successful policy call measured on the docs'
# default model deepseek-flash via llm.cosens.ai, 2026-11-06: 139 tokens for a decision,
# 294 for a decision with a linked feedback turn, 146 for a Recipe match (84 calls).
# The default budget must leave at least 3x headroom over that.
MEASURED_POLICY_COMPLETION_TOKENS = 294


def test_default_decision_budget_fits_a_measured_reasoning_reply(tmp_path):
    from cosens.decision import DEFAULT_DECISION_MAX_OUTPUT_TOKENS
    assert DEFAULT_DECISION_MAX_OUTPUT_TOKENS >= 3 * MEASURED_POLICY_COMPLETION_TOKENS
    budgets = []

    def reasoning_model(request):
        if not request.url.path.endswith('/chat/completions'):
            return native_response(request)
        body = json.loads(request.content)
        budgets.append(body['max_completion_tokens'])
        # Behaves like a reasoning model: a budget below the reply size is cut off.
        if body['max_completion_tokens'] < MEASURED_POLICY_COMPLETION_TOKENS:
            return httpx.Response(200, json={'id': 'd', 'object': 'chat.completion', 'created': 0, 'model': 'model',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''}, 'finish_reason': 'length'}]})
        return _chat(DECLINE)

    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(reasoning_model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(Algorithm()))
    try:
        wrapped.responses.create(model='deepseek-flash', input='Say hi')
        turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        assert budgets == [DEFAULT_DECISION_MAX_OUTPUT_TOKENS]
        assert turn['capture'] == 'skipped' and turn['decision']['reason'] == 'routine'
    finally:
        wrapped.close(); native.close()


def test_decision_model_writes_only_activate_and_reason(tmp_path):
    bodies = []

    def model(request):
        if request.url.path.endswith('/chat/completions'):
            bodies.append(json.loads(request.content))
            return _chat({'activate': True, 'reason': 'multi-step report'})
        return native_response(request)

    algorithm = Algorithm()
    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(algorithm))
    try:
        wrapped.responses.create(model='m', input='Produce a quarterly report')
        schema = bodies[0]['response_format']['json_schema']['schema']
        assert schema['required'] == ['activate', 'reason']
        turn = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
        # The task objective is the user's own text.
        assert turn['objective'] == 'Produce a quarterly report' and turn['execution_id'] == 'execution_1'
    finally:
        wrapped.close(); native.close()


def test_request_without_user_text_makes_no_decision_call(tmp_path):
    calls = []

    def model(request):
        calls.append(request.url.path)
        return native_response(request)

    native = OpenAI(api_key='model-key', http_client=httpx.Client(transport=httpx.MockTransport(model)))
    wrapped = cosens(native, mode='auto', api_key='service-key', state_path=tmp_path / 's.sqlite',
                     _http_transport=httpx.MockTransport(Algorithm()))
    try:
        wrapped.responses.create(model='m', input=[{'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'data:,'}]}])
        assert calls == ['/v1/responses']
        assert wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)['capture'] == 'no_user_text'
    finally:
        wrapped.close(); native.close()
