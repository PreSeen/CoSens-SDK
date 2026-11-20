"""Exercise the Recipe test loop and the real opt-in Python sandbox."""

import json
import threading
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("langgraph.checkpoint.sqlite")

from cosens.rsi import RSIClient
from cosens.rsi.chat_completions import Assistant
from cosens.rsi.python_runner import _installed_image


def tool_reply(name, args):
    return Assistant.model_validate(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": f"call_{name}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }
    )


@contextmanager
def gateway(completion):
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                assert self.path == "/v1/chat/completions"
                assert self.headers["Authorization"] == "Bearer synthetic-fixture"
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                message = completion(None, None, request["messages"], request["tools"])
                response = json.dumps({
                    "choices": [{"message": message.model_dump(exclude_unset=True)}]
                }).encode()
            except (AssertionError, ValueError, KeyError, IndexError, TypeError) as error:
                errors.append(error)
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1"
            if errors:
                raise errors[0]
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize(
    ("mode", "test_status"),
    [
        ("docker", "passed"),
        ("nonzero", "failed"),
        ("unavailable", "unverifiable"),
        ("skipped", "failed"),
    ],
)
def test_recipe_requires_genuine_python_execution(tmp_path, monkeypatch, mode, test_status):
    image = "python:3.12-slim"
    if mode in {"docker", "nonzero"} and _installed_image(image) is None:
        pytest.skip("Docker and the locally installed Python test image are required")
    context = tmp_path / "task-context.md"
    context.write_text("The user requires actually running Python to compute and verify a CSV deliverable.")
    current = tmp_path / "current.csv"
    current.write_text("quantity\n6\n")
    historical = tmp_path / "archived-data.csv"
    historical.write_text("HISTORICAL_RECORD\n77723\n")
    expected = "quantity,doubled\n6,12\n"
    script = r"""
import csv
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
assert os.getuid() == 65534
assert list(Path('/workspace/inputs').rglob('*.csv')) == [source]
try:
    source.open('a')
except OSError:
    pass
else:
    raise AssertionError('input was writable')
try:
    Path('/proc/1/fd/1').open()
except PermissionError:
    pass
else:
    raise AssertionError('runner receipt was accessible')
with source.open() as input_file:
    quantity = int(next(csv.DictReader(input_file))['quantity'])
with Path('verified.csv').open('w', newline='') as output_file:
    writer = csv.writer(output_file, lineterminator='\n')
    writer.writerow(['quantity', 'doubled'])
    writer.writerow([quantity, quantity * int(sys.argv[2])])
print('Read-only input and calculated quantity verified')
"""
    assessed = []

    def completion(_config, _headers, messages, definitions):
        tools = {definition["function"]["name"] for definition in definitions}
        payload = json.loads(messages[1]["content"])
        catalog = payload["evidence_catalog"]
        responses = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "tool"
        ]
        if "FeedbackExtraction" in tools:
            return tool_reply("FeedbackExtraction", {"items": []})
        if "Analysis" in tools:
            source = catalog[0]
            return tool_reply("Analysis", {
                "recipe_name": "Compute verified quantities",
                "sufficient_information": True,
                "rubrics": {
                    "requires_python": True,
                    "criteria": [{
                        "id": "verified_csv",
                        "description": "Run Python to calculate and verify the output CSV",
                        "source": "task",
                        "evidence": [{
                            "material_id": source["material_id"],
                            "locator": source["locator"],
                        }],
                    }],
                },
            })
        if "RecipeContent" in tools:
            return tool_reply("RecipeContent", {
                "name": "Compute verified quantities",
                "description": "Calculate and verify CSV quantities",
                "instructions": "Run Python to compute each derived quantity from the supplied input and verify the CSV.",
            })
        if "Assessment" in tools:
            assert "run_python" not in tools
            assert payload["context"]["rubrics"]["requires_python"] is True
            if not responses:
                return tool_reply("read_evidence", {"locator": "test/verified.csv"})
            if len(responses) == 1:
                assert responses[0]["text"] == expected
                return tool_reply("read_evidence", {"locator": "test/executor-trace.json"})
            trace = json.loads(responses[1]["text"])
            assert trace["python_runs"][0]["exit_code"] == 0
            assert "calculated quantity verified" in trace["python_runs"][0]["stdout"]
            assessed.append(True)
            return tool_reply("Assessment", {
                "verifiable": True,
                "scores": [{
                    "criterion_id": "verified_csv",
                    "status": "verified",
                    "score": 1,
                    "evidence": "Verified calculated values in test/verified.csv and the Python receipt in test/executor-trace.json",
                }],
                "reason": "Verified the Python run and calculated output",
            })
        assert "run_python" in tools
        assert payload["context"]["requires_python"] is True
        assert len(catalog) == 1 and catalog[0]["role"] == "input"
        assert catalog[0]["sandbox_path"].endswith("/current.csv")
        if not responses:
            if mode == "skipped":
                return tool_reply("write_output", {"path": "verified.csv", "content": expected})
            source = "raise SystemExit(17)" if mode == "nonzero" else script
            return tool_reply("write_output", {"path": "scripts/verify.py", "content": source})
        if len(responses) == 1 and mode != "skipped":
            return tool_reply("run_python", {
                "path": "scripts/verify.py",
                "args": [catalog[0]["sandbox_path"], "2"],
            })
        return Assistant(role="assistant", content="The verification finished.")

    detached = mode == "docker"
    if not detached:
        monkeypatch.setattr("cosens.rsi.runtime.request_completion", completion)
    with gateway(completion) if detached else nullcontext("http://127.0.0.1:1/v1") as url:
        client = RSIClient(
            data_dir=tmp_path / "db",
            config={
                "model": "fixture",
                "base_url": url,
                "headers": {"Authorization": "Bearer synthetic-fixture"},
                "auto_start": detached,
                **({"test_python_image": image} if mode != "unavailable" else {}),
            },
        )
        accepted = client.execution_create(
            evidence={
                "context_path": str(context),
                "files": [
                    {"path": str(current), "role": "input"},
                    {"path": str(historical), "role": "reference"},
                ],
            },
            idempotency_key="python-test",
        )
        if detached:
            client._queue.wait(accepted["id"], timeout=30)
        else:
            alias, binding = client._queue._model_header_binding
            monkeypatch.setenv(alias, binding)
            client._queue.run_pending()
    execution = client.execution_get(accepted["id"])
    assert execution["status"] == "completed", execution
    assert execution["result"]["test_status"] == test_status, execution
    assert execution["result"]["published"] is (mode == "docker")
    assert bool(assessed) is (mode == "docker")
    assert len(execution["test_results"]) == (2 if test_status == "failed" else 1)
    tested = execution["test_results"][-1]
    trace = json.loads(Path(tested["trace_path"]).read_text())
    assert "HISTORICAL_RECORD" not in json.dumps(trace)
    assert all("scripts/" not in artifact["locator"] for artifact in tested["artifacts"])
    if mode == "docker":
        assert tested["score"] == 1
        assert Path(tested["artifacts"][0]["path"]).read_text() == expected
    elif mode == "nonzero":
        assert trace["python_runs"][0]["exit_code"] == 17
    elif mode == "unavailable":
        assert trace["python_runs"][0]["status"] == "unavailable"
    else:
        assert "python_runs" not in trace
