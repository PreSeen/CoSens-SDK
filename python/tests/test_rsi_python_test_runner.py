"""Exercise Recipe replay with its default native script tools."""

import hashlib
import io
import json
import shutil
import tarfile
import threading
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph.checkpoint.sqlite")

from cosens.rsi import RSIClient
from cosens.rsi import host_runtime
from cosens.rsi.chat_completions import Assistant
from cosens.rsi.models import RubricsContent, parse_stored_rubrics
from cosens.rsi.python_runner import run_host_script


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

def test_stored_rubrics_ignore_obsolete_python_flag():
    criteria = [{
        "id": "verified_csv",
        "description": "Verify the calculated quantities",
        "source": "agent_inference",
        "evidence": [],
        "required": True,
    }]
    legacy = {"criteria": criteria, "pass_threshold": 0.9, "requires_python": True}
    with pytest.raises(ValidationError, match="requires_python"):
        RubricsContent.model_validate(legacy)
    assert parse_stored_rubrics(legacy) == {"criteria": criteria, "pass_threshold": 0.9}
    assert legacy["requires_python"] is True


@pytest.mark.parametrize(
    ("mode", "test_status"),
    [
        ("nonzero", "passed"),
        ("unavailable", "passed"),
        ("rejected", "passed"),
        ("skipped", "failed"),
        ("text", "passed"),
        ("incidental", "passed"),
        ("host-python", "passed"),
        ("host-typescript", "passed"),
        ("legacy-host-python", "passed"),
    ],
)
def test_recipe_replay_with_native_tools(tmp_path, monkeypatch, mode, test_status):
    host_mode = mode in {"host-python", "host-typescript", "legacy-host-python"}
    execution_required = mode in {"host-python", "skipped"}
    criterion_description = (
        "Run the Python check and verify the calculated quantities in the resulting CSV"
        if execution_required
        else "Verify the calculated quantities in the output CSV"
    )
    if mode == "host-typescript":
        bun = shutil.which("bun")
        node = shutil.which("node")
        if not (bun and host_runtime._version(bun)) and not (
            node and host_runtime._node_command(node)
        ):
            pytest.skip("A host Bun or compatible Node.js executable is required")
    monkeypatch.setenv("SYNTHETIC_WORKER_SECRET", "not-for-script")
    if mode == "unavailable":
        def unavailable(*_args):
            raise host_runtime.HostRuntimeUnavailable("Test interpreter unavailable")

        monkeypatch.setattr("cosens.rsi.python_runner.host_command", unavailable)
    python_quote = "Run Python to compute and verify the CSV deliverable."
    outcome_requirement = (
        "Compute the totals, deliver a CSV summary and a Markdown report, "
        "and check the numbers agree."
    )
    if mode == "incidental":
        context = tmp_path / "task-context.json"
        context.write_text(json.dumps({
            "version": 1,
            "messages": [
                {"type": "user", "text": outcome_requirement},
                {"type": "assistant", "content": python_quote},
            ],
            "feedback": "",
        }))
    else:
        context = tmp_path / "task-context.md"
        context.write_text(python_quote if execution_required else outcome_requirement)
    current = tmp_path / "current.csv"
    current.write_text("quantity\n6\n")
    historical = tmp_path / "archived-data.csv"
    historical.write_text("HISTORICAL_RECORD\n77723\n")
    expected = "quantity,doubled\n6,12\n"
    host_scripts = {
        "host-python": (
            "run_python", "scripts/host.py",
            r"""
import os
import sys
from pathlib import Path

assert 'SYNTHETIC_WORKER_SECRET' not in os.environ
value = int(Path(sys.argv[1]).read_text().splitlines()[1])
Path('verified.csv').write_text(f'quantity,doubled\n{value},{value * 2}\n')
print('Calculated on host')
""",
        ),
        "host-typescript": (
            "run_typescript", "scripts/host.ts",
            r"""
import { readFileSync, writeFileSync } from 'node:fs';

if (process.env.SYNTHETIC_WORKER_SECRET) throw new Error('Host credential was passed');
const value: number = Number(readFileSync(process.argv[2], 'utf8').split('\n')[1]);
writeFileSync('verified.csv', `quantity,doubled\n${value},${value * 2}\n`);
console.log('Calculated on host');
""",
        ),
    }
    assessed = []

    def completion(_config, _headers, messages, definitions):
        tools = {definition["function"]["name"] for definition in definitions}
        payload = json.loads(messages[1]["content"])
        catalog = payload["evidence_catalog"]
        responses = [
            json.loads(message["content"])
            if message["content"].startswith("{")
            else message["content"]
            for message in messages
            if message["role"] == "tool"
        ]
        if "FeedbackExtraction" in tools:
            return tool_reply("FeedbackExtraction", {"items": []})
        if "Analysis" in tools:
            source = catalog[0]
            evidence = {
                "material_id": source["material_id"],
                "locator": source["locator"],
            }
            assert "requires_python" not in json.dumps(definitions)
            return tool_reply("Analysis", {
                "recipe_name": "Compute verified quantities",
                "sufficient_information": True,
                "rubrics": {
                    "criteria": [{
                        "id": "verified_csv",
                        "description": criterion_description,
                        "source": "task",
                        "evidence": [evidence],
                    }],
                },
            })
        if "RecipeContent" in tools:
            assert "requires_python" not in payload["context"]["analysis"]["rubrics"]
            return tool_reply("RecipeContent", {
                "name": "Compute verified quantities",
                "description": "Calculate and verify CSV quantities",
                "instructions": "Compute each derived quantity from the supplied input and verify the CSV.",
            })
        if "Assessment" in tools:
            assert "run_python" not in tools
            assert "run_typescript" not in tools
            assert "requires_python" not in payload["context"]["rubrics"]
            if not responses:
                return tool_reply("read_evidence", {"locator": "test/verified.csv"})
            assert responses[0]["text"] == expected
            if host_mode or execution_required or mode in {"unavailable", "rejected", "nonzero"}:
                if len(responses) == 1:
                    return tool_reply("read_evidence", {"locator": "test/executor-trace.json"})
                trace = json.loads(responses[1]["text"])
                if host_mode:
                    key = "typescript_runs" if mode == "host-typescript" else "python_runs"
                    assert trace[key][0]["exit_code"] == 0
                    assert "Calculated on host" in trace[key][0]["stdout"]
                elif mode == "skipped":
                    assert "python_runs" not in trace
                elif mode == "nonzero":
                    assert trace["python_runs"][0]["exit_code"] == 17
                else:
                    assert trace["python_runs"][0]["status"] == mode
                    assert trace["python_runs"][0]["exit_code"] is None
            assessed.append(True)
            return tool_reply("Assessment", {
                "verifiable": True,
                "scores": [{
                    "criterion_id": "verified_csv",
                    "status": "verified",
                    "score": 0 if mode == "skipped" else 1,
                    "evidence": (
                        "The required Python execution is absent from the executor trace"
                        if mode == "skipped"
                        else "Verified calculated values in test/verified.csv"
                    ),
                }],
                "reason": (
                    "No observed execution for a required check"
                    if mode == "skipped"
                    else "Verified the calculated output"
                ),
            })
        assert {"run_python", "run_typescript"} <= tools
        assert "requires_python" not in payload["context"]
        assert payload["context"]["criteria"] == [{
            "id": "verified_csv",
            "description": criterion_description,
            "required": True,
        }]
        assert len(catalog) == 1 and catalog[0]["role"] == "input"
        assert "sandbox_path" not in catalog[0]
        assert catalog[0]["input_path"].startswith("../inputs/")
        if host_mode:
            language_mode = "host-typescript" if mode == "host-typescript" else "host-python"
            tool, script_path, source = host_scripts[language_mode]
            if not responses:
                return tool_reply("write_output", {"path": script_path, "content": source})
            if len(responses) == 1:
                return tool_reply(tool, {
                    "path": script_path,
                    "args": [catalog[0]["input_path"]],
                })
            assert responses[1]["exit_code"] == 0, responses[1]
            return Assistant(role="assistant", content="The verification finished.")
        if not responses:
            if mode in {"skipped", "text", "incidental"}:
                return tool_reply("write_output", {"path": "verified.csv", "content": expected})
            if mode == "rejected":
                return tool_reply("run_python", {"path": "scripts/missing.py"})
            return tool_reply("write_output", {
                "path": "scripts/verify.py",
                "content": "raise SystemExit(17)",
            })
        if len(responses) == 1 and mode in {"unavailable", "nonzero"}:
            return tool_reply("run_python", {
                "path": "scripts/verify.py",
                "args": [catalog[0]["input_path"]],
            })
        if (mode == "rejected" and len(responses) == 1) or (
            mode in {"unavailable", "nonzero"} and len(responses) == 2
        ):
            return tool_reply("write_output", {"path": "verified.csv", "content": expected})
        return Assistant(role="assistant", content="The verification finished.")

    detached = mode == "host-python"
    if not detached:
        monkeypatch.setattr("cosens.rsi.runtime.request_completion", completion)
    with gateway(completion) if detached else nullcontext("http://127.0.0.1:1/v1") as url:
        client = RSIClient(
            data_dir=tmp_path / "db",
            model_id="fixture",
            base_url=url,
            api_key="synthetic-fixture",
            config={"auto_start": False} if not detached else None,
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
        if mode == "legacy-host-python":
            store = client._queue.store
            with store.transaction() as conn:
                queued = store.get_in(conn, accepted["id"], "execution")
                queued["runtime_config"].update(
                    test_script_mode="host", test_python_image="python:3.12-slim"
                )
                store.save(conn, queued)
        if detached:
            client._queue.wait(accepted["id"], timeout=30)
        else:
            alias, binding = client._queue._model_header_binding
            monkeypatch.setenv(alias, binding)
            client._queue.run_pending()
    execution = client.execution_get(accepted["id"])
    assert execution["status"] == "completed", execution
    assert execution["result"]["test_status"] == test_status, execution
    assert execution["result"]["published"] is (test_status == "passed")
    assert len(assessed) == (2 if test_status == "failed" else 1)
    assert len(execution["test_results"]) == (2 if test_status == "failed" else 1)
    tested = execution["test_results"][-1]
    trace = json.loads(Path(tested["trace_path"]).read_text())
    assert "HISTORICAL_RECORD" not in json.dumps(trace)
    assert all("scripts/" not in artifact["locator"] for artifact in tested["artifacts"])
    if test_status == "passed":
        assert tested["score"] == 1
        assert Path(tested["artifacts"][0]["path"]).read_text() == expected
        assert client.recipe_get(execution["recipe_id"])["active_version_id"] == tested["version_id"]
    if mode in {"incidental", "text", "skipped"}:
        assert "python_runs" not in trace


def test_public_config_rejects_obsolete_script_settings(tmp_path):
    with pytest.raises(ValidationError) as error:
        RSIClient(
            data_dir=tmp_path,
            config={"test_script_mode": "host", "test_python_image": "python:3.12-slim"},
        )
    assert {issue["loc"] for issue in error.value.errors()} == {
        ("test_script_mode",),
        ("test_python_image",),
    }


def test_host_script_failure_does_not_publish_staged_outputs(tmp_path):
    workspace = tmp_path / "output"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "failed.py").write_text(
        "from pathlib import Path\n"
        "Path('bad.txt').write_text('failed')\n"
        "raise SystemExit(17)\n"
    )
    result = run_host_script(
        "scripts/failed.py", [], workspace, [],
        SimpleNamespace(max_file_bytes=4096, max_bundle_bytes=8192),
        lambda: None,
        language="python", data_dir=tmp_path,
    )
    assert result["exit_code"] == 17
    assert not (workspace / "bad.txt").exists()


def test_host_node_is_provisioned_once_from_verified_archive(tmp_path, monkeypatch):
    release = host_runtime._NODE_RELEASE
    name = f"node-{release}-linux-x64"
    payload = b"synthetic Node.js executable"
    archive_contents = io.BytesIO()
    with tarfile.open(fileobj=archive_contents, mode="w:gz") as archive:
        entry = tarfile.TarInfo(f"{name}/bin/node")
        entry.size = len(payload)
        archive.addfile(entry, io.BytesIO(payload))
    data = archive_contents.getvalue()
    urls = []

    def download(url, *, timeout):
        urls.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(host_runtime.platform, "system", lambda: "Linux")
    monkeypatch.setattr(host_runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(host_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setattr(host_runtime, "urlopen", download)
    monkeypatch.setattr(host_runtime, "_version", lambda _path: release)
    monkeypatch.setitem(
        host_runtime._NODE_ASSETS, ("Linux", "x64"),
        ("linux-x64", hashlib.sha256(data).hexdigest()),
    )
    command = host_runtime.host_command("typescript", tmp_path, lambda: None)
    assert Path(command[0]).read_bytes() == payload
    assert host_runtime.host_command("typescript", tmp_path, lambda: None) == command
    assert urls == [f"https://nodejs.org/dist/{release}/{name}.tar.gz"]
    monkeypatch.setitem(
        host_runtime._NODE_ASSETS, ("Linux", "x64"), ("linux-x64", "0" * 64)
    )
    with pytest.raises(host_runtime.HostRuntimeUnavailable, match="Could not provision"):
        host_runtime.host_command("typescript", tmp_path / "bad-checksum", lambda: None)
    assert not list((tmp_path / "bad-checksum").rglob("node.tar.gz"))
    assert not (tmp_path / "bad-checksum/runtimes/node" / release / "linux-x64/node").exists()
