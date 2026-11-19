"""Model request settings: completion budget, User-Agent and structured-output feedback."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import BaseModel

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
    request_completion(ModelConfig(model="m", base_url=base_url), "k", [], [])
    request_completion(
        ModelConfig(model="m", base_url=base_url, max_tokens=16384), "k", [], []
    )
    assert "max_tokens" not in seen[0]["body"]
    assert seen[1]["body"]["max_tokens"] == 16384


def test_requests_name_the_sdk_in_the_user_agent(endpoint):
    base_url, seen = endpoint
    request_completion(ModelConfig(model="m", base_url=base_url), "k", [], [])
    agent = seen[0]["headers"]["User-Agent"]
    assert agent.startswith("cosens-sdk-python/")
    assert "urllib" not in agent.lower()


def test_public_config_carries_budget_and_timeout(tmp_path):
    client = RSIClient(
        data_dir=tmp_path,
        config={
            "model": "m",
            "base_url": "http://127.0.0.1:9/v1",
            "auto_start": False,
            "max_tokens": 16384,
            "timeout": 600,
        },
    )
    model = client._queue.config.model
    assert (model.max_tokens, model.timeout) == (16384, 600)
    default = RSIClient(
        data_dir=tmp_path / "default",
        config={"model": "m", "base_url": "http://127.0.0.1:9/v1", "auto_start": False},
    )
    assert default._queue.config.model.max_tokens is None


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
        Analysis, json.dumps({"hypothesis": "None", "criteria": json.dumps([{"id": "c", "required": True}])})
    )
    assert problem is None and parsed.hypothesis is None
    parsed, problem = parse_structured(
        Analysis, json.dumps({"hypothesis": "None", "criteria": json.dumps([{"id": "c"}])})
    )
    assert "criteria.0.required" in problem and "valid list" not in problem
