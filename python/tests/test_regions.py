import httpx
import pytest

from cosens import CoSensClient
from cosens.state import TurnStore


@pytest.mark.asyncio
async def test_us_routes_all_transports_and_freezes_at_client_creation(monkeypatch):
    monkeypatch.setenv('COSENS_REGION','us')
    requests=[]
    def server(request):
        requests.append(request)
        return httpx.Response(200,json={'status':'completed','run_id':'r1'})
    async with CoSensClient(api_key='regional-key',_http_transport=httpx.MockTransport(server)) as client:
        monkeypatch.delenv('COSENS_REGION')
        client.sync_atoms.get_task('t1')
        await client.atoms.get_task('t1')
        await client.atoms.get_execution('execution1')
    assert [str(r.url) for r in requests]==[
        'https://rsi.cosens.ai/api/tasks/t1','https://rsi.cosens.ai/api/tasks/t1',
        'https://rsi.cosens.ai/api/executions/execution1']
    assert all(r.headers['authorization']=='Bearer regional-key' for r in requests)
    assert all(not any(k.lower().replace('_','-')=='x-user-id' for k in r.headers) for r in requests)


@pytest.mark.parametrize('region', ['eu', 'https://untrusted.example'])
def test_unknown_region_is_rejected_before_any_client_is_created(monkeypatch, region):
    monkeypatch.setenv('COSENS_REGION',region)
    with pytest.raises(ValueError,match='COSENS_REGION'):
        CoSensClient(api_key='regional-key')


def test_client_uses_key_for_us(monkeypatch):
    monkeypatch.setenv('COSENS_REGION', 'us')
    monkeypatch.setenv('COSENS_API_KEY', 'shared-key')
    client = CoSensClient()
    try:
        assert client._transport.api_key == 'shared-key'
        assert client._sync_transport.api_key == 'shared-key'
    finally:
        client.close()


def test_client_falls_back_to_generic_key_and_explicit_key_wins(monkeypatch):
    monkeypatch.setenv('COSENS_REGION', 'us')
    monkeypatch.setenv('COSENS_API_KEY', 'fallback-key')
    fallback = CoSensClient()
    explicit = CoSensClient(api_key='explicit-key')
    try:
        assert fallback._transport.api_key == 'fallback-key'
        assert explicit._transport.api_key == 'explicit-key'
    finally:
        fallback.close()
        explicit.close()


def test_service_identities_do_not_share_turns_with_the_same_key(tmp_path):
    path=tmp_path/'shared.sqlite3'
    old_turns=TurnStore(path,principal='same-key',service_id='old-service'); us_turns=TurnStore(path,principal='same-key')
    try:
        old_turns.put('turn-1',{'run_id':'old-run'})
        assert us_turns.get('turn-1') is None
        us_turns.put('turn-1',{'run_id':'us-run'})
        assert old_turns.get('turn-1')=={'run_id':'old-run'}
        assert us_turns.get('turn-1')=={'run_id':'us-run'}
    finally:
        old_turns.close(); us_turns.close()
