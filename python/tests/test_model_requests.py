"""Model request settings: completion budget, User-Agent and structured-output feedback."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import BaseModel, ValidationError

from cosens.rsi import RSIClient
from cosens.rsi.chat_completions import request_completion
from cosens.rsi.models import ModelConfig
from cosens.rsi.runtime import parse_structured


@pytest.fixture
def endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"headers": dict(self.headers), "body": body})
            data = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen
    server.shutdown()


def test_budget_is_left_to_the_endpoint_unless_configured(endpoint):
    base_url, seen = endpoint
    request_completion(ModelConfig(model="m", base_url=base_url), {}, [], [])
    request_completion(
        ModelConfig(model="m", base_url=base_url, max_tokens=16384), {}, [], []
    )
    assert "max_tokens" not in seen[0]["body"]
    assert seen[1]["body"]["max_tokens"] == 16384


def test_requests_name_the_sdk_in_the_user_agent(endpoint):
    base_url, seen = endpoint
    request_completion(
        ModelConfig(model="m", base_url=base_url), {"X-Api-Key": "test-token"}, [], []
    )
    agent = seen[0]["headers"]["User-Agent"]
    assert agent.startswith("cosens-sdk-python/")
    assert "urllib" not in agent.lower()
    assert seen[0]["headers"]["X-Api-Key"] == "test-token"
    assert "Authorization" not in seen[0]["headers"]


def test_public_config_carries_budget_and_timeout(tmp_path):
    connection = {"model_id": "m", "api_key": "fixture-key", "base_url": "http://127.0.0.1:9/v1"}
    client = RSIClient(
        data_dir=tmp_path,
        **connection,
        config={
            "auto_start": False,
            "max_tokens": 16384,
            "timeout": 600,
        },
    )
    model = client._queue.config.model
    assert (model.max_tokens, model.timeout) == (16384, 600)
    default = RSIClient(data_dir=tmp_path / "default", **connection)
    assert default._queue.config.model.max_tokens is None
    for field in connection:
        partial = {name: value for name, value in connection.items() if name != field}
        with pytest.raises(ValidationError, match=field):
            RSIClient(data_dir=tmp_path, **partial)


def test_recover_leaves_pending_tasks_for_their_original_model_key(tmp_path):
    path = tmp_path / "context.md"
    path.write_text("Task transcript")
    options = {
        "data_dir": tmp_path, "model_id": "m", "base_url": "http://127.0.0.1:9/v1",
        "config": {"auto_start": False},
    }
    client = RSIClient(**options, api_key="a")
    execution = client.execution_create(
        evidence={"context_path": str(path), "files": []}, idempotency_key="example"
    )
    other = RSIClient(**options, api_key="b")
    assert other.recover() == {"started": 0}
    assert other.recover(recipe_id=execution["recipe_id"]) == {"started": 0}
    assert RSIClient(data_dir=tmp_path).recover() == {"started": 0}
    assert client.execution_get(execution["id"])["status"] == "queued"


class Criterion(BaseModel):
    id: str
    required: bool


class Result(BaseModel):
    name: str
    criteria: list[Criterion]


def test_nested_objects_sent_as_json_strings_are_accepted():
    arguments = json.dumps(
        {"name": "n", "criteria": json.dumps([{"id": "c1", "required": True}])}
    )
    parsed, problem = parse_structured(Result, arguments)
    assert problem is None
    assert parsed.criteria[0].id == "c1"


def test_invalid_result_names_the_fields_to_fix():
    parsed, problem = parse_structured(Result, json.dumps({"criteria": [{"id": 1}]}))
    assert parsed is None
    assert "name" in problem and "criteria.0.required" in problem
    parsed, problem = parse_structured(Result, "{not json")
    assert parsed is None and "not valid JSON" in problem


class Analysis(BaseModel):
    hypothesis: str | None
    criteria: list[Criterion]


def test_null_spelled_as_text_is_decoded_and_remaining_errors_are_reported():
    parsed, problem = parse_structured(
        Analysis,
        json.dumps(
            {
                "hypothesis": "None",
                "criteria": json.dumps([{"id": "c", "required": True}]),
            }
        ),
    )
    assert problem is None and parsed.hypothesis is None
    parsed, problem = parse_structured(
        Analysis,
        json.dumps({"hypothesis": "None", "criteria": json.dumps([{"id": "c"}])}),
    )
    assert "criteria.0.required" in problem and "valid list" not in problem
