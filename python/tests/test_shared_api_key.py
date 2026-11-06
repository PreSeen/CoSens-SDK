"""One CoSens API Key covers the developer's model client and CoSens HTTP."""
import json
import httpx
import pytest
from openai import AsyncOpenAI, OpenAI

from cosens import CoSensClient, cosens
from test_explicit_recipe_protocol import Backend, native_response

MODEL_BASE_URL = "https://llm.cosens.ai/v1"


def _assert_plain_bearer(request):
    assert request.headers['Authorization'] == 'Bearer shared-user-key'
    assert not [name for name in request.headers if name.lower().endswith('auth-type')]


def test_wrapped_client_records_model_without_credentials_in_body(monkeypatch, tmp_path):
    monkeypatch.setenv("COSENS_API_KEY", "shared-user-key")
    monkeypatch.setenv("COSENS_REGION", "us")
    backend = Backend(); seen = []
    def gateway(request):
        _assert_plain_bearer(request)
        seen.append(request)
        if request.url.path.endswith('/chat/completions'):
            body = json.loads(request.content)
            assert body['response_format']['json_schema']['name'] == 'cosens_decision'
            assert 'text' not in body and 'store' not in body
        return native_response(request)
    native = OpenAI(base_url=MODEL_BASE_URL, api_key='shared-user-key', http_client=httpx.Client(transport=httpx.MockTransport(gateway)))
    def algorithm(request):
        assert request.headers['Authorization'] == 'Bearer shared-user-key'
        assert b'shared-user-key' not in request.content
        return backend(request)
    client = cosens(native, mode='auto', state_path=tmp_path/'state.sqlite', _http_transport=httpx.MockTransport(algorithm))
    response = client.responses.create(model='public-model', input='Write report', store=False)
    assert response.output_text == 'final artifact'
    assert [request.url.path for request in seen] == ['/v1/chat/completions', '/v1/responses']
    execution = next(body for _, path, body in backend.seen if path.endswith('/executions'))
    assert execution['model'] == 'public-model' and 'model_api' not in execution
    assert backend.generations == 0
    client.cosens.generate_recipe()
    assert backend.generations == 1
    client.close(); native.close()


def test_atomic_calls_send_only_model_and_use_transport_key(tmp_path):
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={})
    client = CoSensClient(api_key='shared-user-key', state_path=tmp_path/'state.sqlite', _http_transport=httpx.MockTransport(server))
    client.sync_atoms.start_recipe_candidate('task', revision_id='rev', key='confirmed', model='public-model')
    client.sync_atoms.resume_recipe_candidate('candidate', model='public-model')
    assert json.loads(seen[0].content) == {'data': {'revision_id': 'rev', 'model': 'public-model'}}
    assert json.loads(seen[1].content) == {'data': {'model': 'public-model'}}
    assert all(r.headers['Authorization'] == 'Bearer shared-user-key' and b'shared-user-key' not in r.content for r in seen)
    with pytest.raises(TypeError):
        client.sync_atoms.start_recipe_candidate('task', revision_id='rev', key='old', model_api={})
    client.close()


@pytest.mark.asyncio
async def test_async_client_reuses_key_for_decision_and_explicit_generation(monkeypatch, tmp_path):
    monkeypatch.setenv('COSENS_API_KEY', 'shared-user-key')
    monkeypatch.setenv('COSENS_REGION', 'us')
    backend = Backend(); seen = []
    def gateway(request):
        seen.append(request)
        _assert_plain_bearer(request)
        return native_response(request)
    native = AsyncOpenAI(base_url=MODEL_BASE_URL, api_key='shared-user-key', http_client=httpx.AsyncClient(transport=httpx.MockTransport(gateway)))
    wrapped = cosens(native, mode='auto', state_path=tmp_path/'async.sqlite', _http_transport=httpx.MockTransport(backend))
    result = await wrapped.responses.create(model='public-model', input='Write report', store=False)
    assert result.output_text == 'final artifact' and backend.generations == 0
    await wrapped.cosens.generate_recipe()
    assert backend.generations == 1 and seen[0].url.path == '/v1/chat/completions'
    await wrapped.close(); await native.close()
