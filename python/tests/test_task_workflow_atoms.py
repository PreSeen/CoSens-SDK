import json

import httpx
import pytest

from cosens import CoSensClient


@pytest.mark.asyncio
async def test_task_workflow_requests_and_response_forwarding():
    calls = []
    def server(request):
        calls.append((request.method, request.url.path, json.loads(request.content) if request.content else None))
        return httpx.Response(200, json={'id': 'test', 'status': 'ready'})
    async with CoSensClient(api_key='key', _http_transport=httpx.MockTransport(server)) as client:
        task = await client.atoms.create_task(source='new_task', objective='test', key='task-key')
        await client.atoms.start_execution('task-1', key='execution-key')
        await client.atoms.submit_execution('execution-1', request_sha256='hash', outcome='completed',
                                            artifacts=[{'artifact_id': 'artifact-1', 'role': 'submitted_output'}],
                                            events=[], trace_coverage='partial')
        client.sync_atoms.accept_revision('revision-1', artifact_hashes={'artifact-1': 'hash'})
        await client.atoms.start_recipe_candidate('task-1', revision_id='revision-1', key='recipe-key')
        client.sync_atoms.get_recipe_candidate('recipe-1')
    assert task.status == 'ready'
    assert calls == [
        ('POST', '/api/v1/tasks', {'source': 'new_task', 'artifacts': [], 'objective': 'test'}),
        ('POST', '/api/v1/tasks/task-1/executions', None),
        ('POST', '/api/v1/executions/execution-1/result', {'request_sha256': 'hash', 'outcome': 'completed', 'artifacts': [{'artifact_id': 'artifact-1', 'role': 'submitted_output'}], 'events': [], 'trace_coverage': 'partial'}),
        ('POST', '/api/v1/revisions/revision-1/acceptance', {'data': {'artifact_hashes': {'artifact-1': 'hash'}}}),
        ('POST', '/api/v1/tasks/task-1/recipe-candidates', {'data': {'revision_id': 'revision-1', 'include_memory': False}}),
        ('GET', '/api/v1/recipe-candidates/recipe-1', None),
    ]


@pytest.mark.asyncio
async def test_document_upload_and_status_requests():
    calls = []
    def server(request):
        calls.append(request)
        return httpx.Response(200, json={'document_id': 'doc'})
    async with CoSensClient(api_key='key', _http_transport=httpx.MockTransport(server)) as client:
        await client.atoms.upload_document(filename='notes.txt', mime_type='text/plain', content=b'hello', wait_for_processing=False)
        client.sync_atoms.get_document_status(['doc'])
    assert calls[0].url.path == '/api/v1/document/upload'
    assert calls[0].headers['content-type'].startswith('multipart/form-data; boundary=')
    assert b'hello' in calls[0].content and b'false' in calls[0].content
    assert calls[1].url.path == '/api/v1/document/status'
    assert json.loads(calls[1].content) == {'document_ids': ['doc']}
