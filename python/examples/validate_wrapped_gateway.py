"""Real gateway / local fake model check for transparent and explicit SDK entrypoints.

Creates synthetic Runs for auto=true and explicit run. No real model is called.
"""

import argparse
import asyncio
import getpass
import json
import os
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
from agents import Agent
from openai import AsyncOpenAI, OpenAI

from cosens import cosens
from cosens._config import resolve_service


async def check(key, report):
    calls = report["requests"]
    def observe(response):
        request = response.request
        identity = any(k.lower().replace('_', '-') == 'x-user-id' for k in request.headers)
        calls.append({"method":request.method, "path":request.url.path, "http_status":response.status_code, "identity_header_sent":identity, "request_id":response.headers.get('x-request-id')})
        assert not identity
    async def aobserve(response):
        observe(response)

    with tempfile.TemporaryDirectory(prefix="cosens-sdk-gateway-") as directory:
        for asynchronous in (False, True):
            for scenario in ("disable", "auto_false", "auto_true"):
                label = f"{scenario}_{'async' if asynchronous else 'sync'}"
                model_calls = []
                def model_server(request):
                    body = json.loads(request.content)
                    model_calls.append(body)
                    assert request.headers['authorization'] == 'Bearer offline-model-only'
                    if body.get("text", {}).get("format", {}).get("name") == "cosens_decision":
                        text = json.dumps({"activate": scenario == "auto_true", "objective": label + " synthetic validation " + uuid.uuid4().hex, "task_family":"general", "reason":"local test decision"})
                    else:
                        assert "CoSens frozen context" in body.get("instructions", "")
                        text = "local fake model result"
                    return httpx.Response(200, json={"id": "response-" + uuid.uuid4().hex, "object":"response", "created_at":0, "model":"offline-test", "status":"completed", "output":[{"id":"m1", "type":"message", "role":"assistant", "status":"completed", "content":[{"type":"output_text", "text":text, "annotations":[]}]}]})
                native = (AsyncOpenAI if asynchronous else OpenAI)(api_key="offline-model-only", http_client=(httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(model_server)))
                wrapped = cosens(native, api_key=key, mode="disable" if scenario == "disable" else "auto", state_path=Path(directory) / f"{label}.sqlite3")
                transport = wrapped._runtime.client._transport if asynchronous else wrapped._runtime.client._sync_transport
                transport.client.event_hooks['response'].append(aobserve if asynchronous else observe)
                start = len(calls)
                try:
                    params = {"model":"offline-test", "input":label + " gateway check " + uuid.uuid4().hex}
                    reply = await wrapped.responses.create(**params) if asynchronous else wrapped.responses.create(**params)
                    assert reply.output_text == "local fake model result"
                    state = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
                    assert state['state'] == 'completed'
                    assert bool(state['run_id']) == (scenario == 'auto_true')
                    if scenario != 'auto_true':
                        assert [r['path'] for r in calls[start:]] == ['/api/v1/recipes/active', '/api/v1/memory/recall']
                    assert len(model_calls) == (1 if scenario == 'disable' else 2)
                    report[label] = {"status":"passed", "run_id":state['run_id']}
                finally:
                    if asynchronous:
                        await wrapped.close()
                    else:
                        wrapped.close()

        class LocalRunner:
            @staticmethod
            async def run(agent, user_input, **kwargs):
                assert "CoSens" in agent.instructions
                return SimpleNamespace(final_output="local fake Agent result", interruptions=[])
        agent = Agent(name="sdk-gateway-check", instructions="Synthetic test only", model="offline-test")
        wrapped = cosens(agent, api_key=key, mode="disable", state_path=Path(directory) / "agent.sqlite3", _runner=LocalRunner)
        wrapped._client._transport.client.event_hooks['response'].append(aobserve)
        try:
            reply = await wrapped.run(agent, "Synthetic SDK explicit run check " + uuid.uuid4().hex)
            state = wrapped.cosens.get_turn(wrapped.cosens.last_turn_id)
            assert reply.final_output == "local fake Agent result" and state['state'] == 'completed'
            report['explicit_run'] = {"status":"passed", "run_id":state['run_id']}
        finally:
            await wrapped.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    key = os.getenv('RSI_API_KEY') or os.getenv('COSENS_API_KEY') or getpass.getpass('Friday API Key (hidden): ')
    service = resolve_service()
    report = {"model_execution":"local fake only", "gateway":service.url, "region":service.region, "requests":[]}
    try:
        asyncio.run(check(key, report))
        report['result'] = 'passed'
    except Exception as exc:
        report['result'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'.replace(key, '<redacted>')
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2).replace(key, '<redacted>'))
        print(json.dumps({k:v for k,v in report.items() if k!='requests'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
