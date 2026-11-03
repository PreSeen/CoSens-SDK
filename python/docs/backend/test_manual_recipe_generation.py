"""Contract tests for the backend patch; no database server or model needed.

Run with PYTHONPATH=<patched-backend>/src using the backend's test environment.
The result handler is real; only artifact resolution and the enqueue hook are
replaced to isolate the generation decision and immutable receipt contract.
"""
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, StrictBool
import pytest
import cosens_rsi.task_workflow as workflow


class Service(workflow.TaskWorkflow):
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.enqueued = []
        self.db.executescript('''
            CREATE TABLE workflow_tasks(id TEXT, principal TEXT);
            CREATE TABLE workflow_executions(id TEXT, task_id TEXT, kind TEXT, status TEXT,
                request_json TEXT, request_sha TEXT, result_json TEXT);
            CREATE TABLE workflow_revisions(id TEXT, task_id TEXT, execution_id TEXT, result_json TEXT);
            INSERT INTO workflow_tasks VALUES('task', 'owner');
            INSERT INTO workflow_executions VALUES('execution','task','original','awaiting_execution','{}','hash',NULL);
        ''')

    @contextmanager
    def _db(self):
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    _workflow_json = staticmethod(lambda value: json.dumps(value, sort_keys=True))
    _workflow_artifacts = staticmethod(lambda db, principal, refs: refs)

    def _enqueue_initial_workflow_recipe(self, db, task, principal, revision, execution, result):
        self.enqueued.append(revision)
        return {'revision_id': revision, 'recipe_candidate_id': 'candidate'}


def body(**options):
    return {'request_sha256': 'hash', 'outcome': 'completed',
            'artifacts': [{'artifact_id': 'output', 'role': 'submitted_output'}],
            'events': [], 'trace_coverage': 'partial', **options}


def test_submission_only_records_evidence_and_receipt_is_immutable():
    service = Service()
    try:
        data = body()
        receipt = service.submit_workflow_execution('execution', 'owner', data)
        assert service.enqueued == []
        assert 'initial_recipe_candidate_id' not in receipt
        assert 'generate_recipe' not in receipt
        assert receipt['revision_id']
        assert service.submit_workflow_execution('execution', 'owner', data) == receipt
        assert service.enqueued == []
        assert service.db.execute('SELECT COUNT(*) FROM workflow_revisions').fetchone()[0] == 1
        with pytest.raises(ValueError):
            service.submit_workflow_execution('execution', 'owner', {**data, 'trace_coverage': 'full'})
        assert service.get_workflow_execution('execution', 'owner')['result'] == receipt
    finally:
        service.db.close()


def result_model():
    # Isolate the actual HTTP schema without constructing the global production
    # RSIService/database during import of api.py.
    source = Path(workflow.__file__).with_name('api.py').read_text()
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and
             node.name in {'WorkflowArtifactBody', 'WorkflowEventBody', 'WorkflowResultBody'}]
    namespace = {'BaseModel': BaseModel, 'ConfigDict': ConfigDict, 'Literal': Literal, 'StrictBool': StrictBool}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<backend HTTP schemas>', 'exec'), namespace)
    return namespace['WorkflowResultBody']


@pytest.mark.parametrize('obsolete_flag', [False, True])
def test_http_needs_no_generation_flag_and_never_generates(obsolete_flag):
    service = Service()
    Result = result_model()
    app = FastAPI()

    @app.post('/result')
    def submit(value: Result):
        return service.submit_workflow_execution('execution', 'owner', value.model_dump(exclude_unset=True))

    try:
        with TestClient(app) as client:
            assert client.post('/result', json=body(generate_recipe=obsolete_flag)).status_code == 422
            response = client.post('/result', json=body())
            assert response.status_code == 200, response.text
            assert 'generate_recipe' not in response.json()
            assert 'initial_recipe_candidate_id' not in response.json()
            assert service.enqueued == []
    finally:
        service.db.close()
