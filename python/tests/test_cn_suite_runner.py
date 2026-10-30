"""Keep the one-click IDEA runner's CN and model environment wiring stable."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "examples/run_all_cn_tests.py"
spec = importlib.util.spec_from_file_location("cn_suite_runner", SCRIPT)
suite = importlib.util.module_from_spec(spec)
spec.loader.exec_module(suite)


def test_cn_suite_forwards_model_credentials_and_uses_cn_key():
    saved = {"RSI_API_KEY": "file-key", "RSI_API_KEY_CN": "cn-key", "OPENAI_API_KEY": "model-key", "OPENAI_MODEL": "model-name"}
    env, key = suite.build_environment(saved, {"COSENS_REGION": "us"})
    assert key == env["RSI_API_KEY"] == env["RSI_API_KEY_CN"] == "cn-key"
    assert env["COSENS_REGION"] == "cn"
    assert env["OPENAI_API_KEY"] == "model-key"
    assert env["OPENAI_MODEL"] == "model-name"


def test_cn_suite_rejects_missing_key():
    with pytest.raises(ValueError, match="RSI_API_KEY_CN"):
        suite.build_environment({}, {})


def test_cn_suite_only_treats_workflow_probe_code_two_as_unavailable():
    assert suite.overall_status([{"name": "python-cn-workflow", "exit_code": 2}]) == ("workflow_not_deployed", 2)
    assert suite.overall_status([{"name": "python-unit", "exit_code": 2}]) == ("failed", 1)
    assert suite.overall_status([{"name": "python-unit", "exit_code": 0}, {"name": "typescript-check", "exit_code": 0}]) == ("passed", 0)
