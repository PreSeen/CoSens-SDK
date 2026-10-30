import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError


async def test_improvement_model_binding_resume_and_attribution_confirmation():
    seen = []
    def server(request):
        seen.append(request)
        return httpx.Response(200, json={"job_id": "j1", "status": "queued"})
    model_api = {"base_url": "https://business.example/v1", "model": "business-model", "api_key": "model-key"}
    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(server)) as client:
        await client.atoms.improve("r1", reason="Check missing rows", feedback="Total is incomplete", key="improve-1", model_api=model_api)
        await client.atoms.resume_improvement("j1", model_api=model_api)
        await client.atoms.resume_improvement("j1")
        await client.atoms.confirm_attribution("j1", candidate_id="c1", candidate_sha256="frozen-hash")
    assert json.loads(seen[0].content) == {"data": {"reason": "Check missing rows", "feedback": "Total is incomplete", "model_api": model_api}}
    assert seen[0].headers["Idempotency-Key"] == "improve-1"
    assert json.loads(seen[1].content) == {"data": {"model_api": model_api}}
    assert not seen[2].content
    assert json.loads(seen[3].content) == {"data": {"candidate_id": "c1", "candidate_sha256": "frozen-hash"}}
    assert seen[3].url.path.endswith("/improvements/j1/attribution/confirm")
    assert all(r.headers["Authorization"] == "Bearer gateway-key" for r in seen)


@pytest.mark.parametrize("method", ["improve", "resume_improvement"])
async def test_model_key_redacted_from_gateway_errors(method):
    def server(request):
        return httpx.Response(401, json={"error": {"message": "Rejected secret-model-key", "code": "secret-model-key"}},
                              headers={"x-request-id": "secret-model-key"})
    api = {"base_url": "https://business.example/v1", "model": "m", "api_key": "secret-model-key"}
    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(server)) as client:
        with pytest.raises(RSIHTTPError) as error:
            if method == "improve":
                await client.atoms.improve("r1", reason="Improve", model_api=api)
            else:
                await client.atoms.resume_improvement("j1", model_api=api)
    assert "secret-model-key" not in str(error.value)
    assert error.value.code == "[REDACTED]" and error.value.request_id == "[REDACTED]"


async def test_invalid_model_config_rejected_before_sending_credentials():
    seen = []
    async with CoSensClient(api_key="gateway-key", _http_transport=httpx.MockTransport(lambda r: seen.append(r))) as client:
        for extra in ({"base_url": "file:///etc/passwd"}, {"api_key": "key\r\n"}, {"agent_argv": ["sh"]}):
            with pytest.raises(ValueError):
                await client.atoms.improve("r1", reason="Improve", model_api={"base_url": "https://business.example/v1", "model": "m", **extra})
    assert not seen
