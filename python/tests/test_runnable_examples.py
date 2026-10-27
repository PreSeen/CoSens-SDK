"""Execute the click-to-run files with real SDKs and only HTTP replaced."""
import json
import runpy
from pathlib import Path

import httpx
import pytest

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
FILES = ["01_transparent.py", "02_explicit_run.py", "03_atomic.py"]


@pytest.fixture
def example_env(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(EXAMPLES))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "model-test-secret")
    monkeypatch.setenv("RSI_API_KEY", "gateway-test-secret")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.example/v1")
    monkeypatch.setenv("COSENS_MODE", "auto")
    # Keep the real SDK persistence, isolated from the developer's state.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


@pytest.mark.parametrize("filename,mode", [(name, "auto") for name in FILES] + [(FILES[0], "disable")])
def test_click_run_uses_real_sdks_and_recalls_before_model(filename, mode, example_env, monkeypatch, capsys):
    monkeypatch.setenv("COSENS_MODE", mode)
    calls = []

    def send(self, request, **kwargs):
        body = json.loads(request.content) if request.content else {}
        calls.append((request.url.path, body))
        assert "x-user-id" not in request.headers and "x_user_id" not in request.headers
        if request.url.host == "model.example":
            assert request.headers["authorization"] == "Bearer model-test-secret"
            decision = body.get("text", {}).get("format", {}).get("name") == "cosens_decision"
            output = json.dumps({"activate": True, "objective": "demo", "task_family": "general", "reason": "reusable"}) if decision else "示例回答完成"
            data = {"id": "resp-demo", "object": "response", "created_at": 0, "model": "test-model", "status": "completed", "output": [{"id": "msg-demo", "type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": output, "annotations": []}]}], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}}
        else:
            assert request.url.host == "friday-rsi-cn.stardust-ai.com"
            assert request.headers["authorization"] == "Bearer gateway-test-secret"
            path = request.url.path
            if path.endswith("/recipes/active"):
                data = {"status": "matched", "recipe_id": "recipe-demo", "bundle": {"summary": "RECALLED_FACT"}}
            elif path.endswith("/memory/recall"):
                data = {"items": []}
            elif path.endswith("/prepare"):
                data = {"run_id": "demo-run", "status": "ready", "recipe": {"summary": "RECALLED_FACT"}, "context": {}}
            elif path.endswith("/session-events"):
                data = {"durable_ack": [e["id"] for e in body["events"]]}
            elif path.endswith("/submit"):
                data = {"run_id": "demo-run", "status": "completed"}
            elif path.endswith("/artifacts"):
                data = {"artifact_id": "demo-artifact", "name": body["name"]}
            elif path.endswith("/trace"):
                data = {"run_id": "demo-run", "session_events": []}
            elif path == "/api/v1/runs/demo-run":
                data = {"run_id": "demo-run", "status": "ready", "snapshot": {"evidence": []}}
            else:
                pytest.fail(f"Unexpected network call: {path}")
        return httpx.Response(200, json=data, request=request)

    async def async_send(self, request, **kwargs):
        return send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    target = EXAMPLES / filename
    assert target.exists(), f"Missing runnable example: {filename}"
    runpy.run_path(str(target), run_name="__main__")
    output = capsys.readouterr().out
    assert "示例回答完成" in output
    assert ("demo-run" in output) == (mode != "disable")
    assert "model-test-secret" not in output and "gateway-test-secret" not in output
    business = [(i, body) for i, (path, body) in enumerate(calls) if path == "/v1/responses" and body.get("text", {}).get("format", {}).get("name") != "cosens_decision"]
    assert len(business) == 1
    assert "RECALLED_FACT" in json.dumps(business[0][1])
    recall_path = "/api/v1/memory/recall" if mode == "disable" else "/api/v1/runs/demo-run"
    assert next(i for i, (path, _) in enumerate(calls) if path == recall_path) < business[0][0]
    assert sum(path == "/v1/responses" for path, _ in calls) == (2 if filename == FILES[0] and mode == "auto" else 1)
    assert any(path.endswith("/submit") for path, _ in calls) == (mode != "disable")
    events = [event for path, body in calls if path.endswith("/session-events") for event in body["events"]]
    if mode != "disable":
        assert any("示例回答完成" in json.dumps(e, ensure_ascii=False) for e in events)
    else:
        assert not events
        assert not any(path.endswith("/prepare") for path, _ in calls)


@pytest.mark.parametrize("filename", FILES)
def test_missing_configuration_stops_before_network(filename, example_env, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    def forbidden(*args, **kwargs):
        pytest.fail("Configuration failure must not make network requests")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    target = EXAMPLES / filename
    assert target.exists(), f"Missing runnable example: {filename}"
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(target), run_name="__main__")
    assert error.value.code == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_env_file_loads_without_cwd_dependency_and_keeps_environment_priority(example_env, monkeypatch, tmp_path):
    import _common
    env = tmp_path / "settings" / ".env"
    env.parent.mkdir()
    env.write_text("OPENAI_API_KEY=file-model-key\nRSI_API_KEY=file-rsi-key\nOPENAI_MODEL=file-model\n")
    monkeypatch.setattr(_common, "ENV_FILE", env)
    monkeypatch.delenv("RSI_API_KEY")
    loaded = _common.settings()
    assert loaded.openai_key == "model-test-secret"
    assert loaded.rsi_key == "file-rsi-key"
    assert loaded.model == "test-model"
    assert "file-rsi-key" not in repr(loaded)
    assert "model-test-secret" not in repr(loaded)


def test_example_errors_do_not_dump_remote_validation_payload(example_env, capsys):
    import _common
    def failure():
        raise ValueError("remote validation payload: secret-from-response")
    with pytest.raises(SystemExit) as error:
        _common.launch(failure)
    assert error.value.code == 1
    output = capsys.readouterr().err
    assert "ValueError" in output
    assert "secret-from-response" not in output
