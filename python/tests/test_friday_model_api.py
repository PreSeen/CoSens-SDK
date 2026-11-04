import pytest

from cosens.model_api import model_connection_data
from cosens import friday_gateway_client


def test_friday_model_configuration_and_credential_redaction():
    config = {"base_url": "https://llm.cosens.ai/v1", "model": "public-model",
              "api_key": "user-secret", "friday_auth_type": "api_key"}
    data, secrets = model_connection_data(model_api=config)
    assert data["model_api"] == config
    assert secrets == ("user-secret",)
    with pytest.raises(ValueError):
        model_connection_data(model_api={**config, "base_url": "https://other.example/v1"})


def test_gateway_client_queries_models_with_user_credential():
    import httpx
    seen = []
    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": "model-id", "object": "model", "created": 0, "owned_by": "friday"}]})
    http = httpx.Client(transport=httpx.MockTransport(handle))
    client = friday_gateway_client("user-key", http_client=http)
    assert next(iter(client.models.list())).id == "model-id"
    assert str(seen[0].url) == "https://llm.cosens.ai/v1/models"
    assert seen[0].headers["Authorization"] == "Bearer user-key"
    assert seen[0].headers["X-Friday-Auth-Type"] == "api_key"
    client.close()


def test_workflow_atoms_send_model_configuration_for_each_algorithm_job():
    from cosens.workflow_atoms import SyncWorkflowAtoms
    class Transport:
        def __init__(self):
            self.calls = []
        def request(self, method, path, **kwargs):
            self.calls.append((path, kwargs))
            return {}
    transport = Transport()
    atoms = SyncWorkflowAtoms()
    atoms._http = transport
    model = {"base_url": "https://llm.cosens.ai/v1", "model": "chosen", "api_key": "secret", "friday_auth_type": "api_key"}
    atoms.start_recipe_candidate('task', revision_id='rev', key='c', model_api=model)
    atoms.resume_recipe_candidate('recipe', model_api=model)
    assert len(transport.calls) == 2
    for _, kwargs in transport.calls:
        assert kwargs['body']['data']['model_api'] == model
        assert kwargs['sensitive_values'] == ('secret',)
