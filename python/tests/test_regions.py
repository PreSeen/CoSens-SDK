import httpx
import pytest

from cosens import CoSensClient
from cosens.session_cursor import CursorStore
from cosens.state import TurnStore


@pytest.mark.parametrize('region,host', [('cn','friday-rsi-cn.stardust-ai.com'),('us','rsi.cosens.ai')])
@pytest.mark.asyncio
async def test_region_routes_all_transports_and_freezes_at_client_creation(monkeypatch, region, host):
    monkeypatch.setenv('COSENS_REGION',region)
    requests=[]
    def server(request):
        requests.append(request)
        return httpx.Response(200,json={'status':'completed','run_id':'r1'})
    async with CoSensClient(api_key='regional-key',_http_transport=httpx.MockTransport(server)) as client:
        monkeypatch.setenv('COSENS_REGION','us' if region=='cn' else 'cn')
        client.sync_atoms.get_run('r1')
        await client.atoms.get_run('r1')
        await client.admin.atoms.publish('j1',expected_revision=1,reason='verified')
    assert [str(r.url) for r in requests]==[
        f'https://{host}/api/v1/runs/r1',f'https://{host}/api/v1/runs/r1',
        f'https://{host}/api/internal/admin/v1/improvements/j1/publish']
    assert all(r.headers['authorization']=='Bearer regional-key' for r in requests)
    assert all(not any(k.lower().replace('_','-')=='x-user-id' for k in r.headers) for r in requests)


def test_unknown_region_is_rejected_before_any_client_is_created(monkeypatch):
    monkeypatch.setenv('COSENS_REGION','https://untrusted.example')
    with pytest.raises(ValueError,match='COSENS_REGION'):
        CoSensClient(api_key='regional-key')


@pytest.mark.parametrize('region,expected', [('cn','cn-key'), ('us','us-key')])
def test_client_uses_key_for_selected_region(monkeypatch, region, expected):
    monkeypatch.setenv('COSENS_REGION', region)
    monkeypatch.setenv('RSI_API_KEY_CN', 'cn-key')
    monkeypatch.setenv('RSI_API_KEY_US', 'us-key')
    monkeypatch.setenv('RSI_API_KEY', 'fallback-key')
    client = CoSensClient()
    try:
        assert client._transport.api_key == expected
        assert client._sync_transport.api_key == expected
        assert client._admin_transport.api_key == expected
    finally:
        client.close()


def test_client_falls_back_to_generic_key_and_explicit_key_wins(monkeypatch):
    monkeypatch.setenv('COSENS_REGION', 'us')
    monkeypatch.setenv('RSI_API_KEY_US', '')
    monkeypatch.setenv('RSI_API_KEY', 'fallback-key')
    fallback = CoSensClient()
    explicit = CoSensClient(api_key='explicit-key')
    try:
        assert fallback._transport.api_key == 'fallback-key'
        assert explicit._transport.api_key == 'explicit-key'
    finally:
        fallback.close()
        explicit.close()


@pytest.mark.asyncio
async def test_regions_do_not_share_turns_or_message_ack_with_same_key_and_session(tmp_path, monkeypatch):
    class Session:
        session_id='same-session'
        def get_items(self):
            return [{'id':'m1','role':'user','content':'hello'}]
    path=tmp_path/'shared.sqlite3'; session=Session()
    monkeypatch.setenv('COSENS_REGION','cn')
    cn_turns=TurnStore(path,principal='same-key'); cn_cursor=CursorStore(path,principal='same-key')
    monkeypatch.setenv('COSENS_REGION','us')
    us_turns=TurnStore(path,principal='same-key'); us_cursor=CursorStore(path,principal='same-key')
    try:
        # The existing CN store stays CN even after the environment changes.
        cn_turns.put('turn-1',{'run_id':'cn-run'})
        cn_snapshot=await cn_cursor.snapshot(session)
        cn_cursor.begin_batch('cn-run',cn_snapshot,cn_cursor.pending(cn_snapshot))
        cn_cursor.ack(cn_snapshot,{'durable_ack':['chat:m1']})
        assert us_turns.get('turn-1') is None
        assert [e['id'] for e in us_cursor.pending(await us_cursor.snapshot(session))]==['chat:m1']
        assert cn_cursor.pending(await cn_cursor.snapshot(session))==[]
        us_turns.put('turn-1',{'run_id':'us-run'})
        assert cn_turns.get('turn-1')=={'run_id':'cn-run'}
        assert us_turns.get('turn-1')=={'run_id':'us-run'}
    finally:
        for store in [cn_turns,us_turns,cn_cursor,us_cursor]: store.close()
