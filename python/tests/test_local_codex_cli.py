import asyncio
import json
import stat
import textwrap
import time

import pytest

from cosens.local import CodexCLIModel, ModelUnavailable

SCHEMA = {"type": "object"}


def fake_codex(tmp_path, body):
    """An executable that records its arguments and stdin, then runs ``body``."""
    path = tmp_path / "codex"
    record = tmp_path / "record.json"
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""\
        import json, os, sys
        args = sys.argv[1:]
        stdin = sys.stdin.read()
        json.dump({{"args": args, "stdin": stdin, "cwd": os.getcwd(), "cwd_files": os.listdir(".")}}, open({str(record)!r}, "w"))
        output = args[args.index("-o") + 1]
        """) + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path, record


SUCCESS = """
    open(output, "w").write('{"ok": true}')
    print(json.dumps({"type": "item.completed", "item": {"type": "error", "message": "config warning"}}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
"""


async def test_runs_isolated_and_returns_the_output_file(tmp_path):
    path, record = fake_codex(tmp_path, SUCCESS)
    model = CodexCLIModel(executable=str(path), model="m1", reasoning_effort="high")
    assert await model.call(instruction="do", schema=SCHEMA, context={"role": "r"}) == {"ok": True}
    seen = json.loads(record.read_text())
    args = seen["args"]
    assert args[0] == "exec" and args[-1] == "-"
    assert "--ephemeral" in args and "--skip-git-repo-check" in args and "--json" in args
    assert args[args.index("-s") + 1] == "read-only"
    assert args[args.index("--disable") + 1] == "hooks"
    assert "mcp_servers={}" in args
    assert 'model_reasoning_effort="high"' in args
    assert args[args.index("-m") + 1] == "m1"
    assert seen["cwd_files"] == [] and seen["cwd"] == args[args.index("-C") + 1]
    assert '"instruction": "do"' in seen["stdin"]
    assert model.identity == "codex:m1"


async def test_config_warning_items_do_not_fail_the_call(tmp_path):
    path, _ = fake_codex(tmp_path, SUCCESS)
    assert await CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={}) == {"ok": True}


async def test_nonzero_exit_is_retryable_unavailable(tmp_path):
    path, _ = fake_codex(tmp_path, "sys.exit(3)\n")
    with pytest.raises(ModelUnavailable) as caught:
        await CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={})
    assert caught.value.code == "upstream_unavailable" and caught.value.retryable


async def test_provider_rejection_is_not_retryable(tmp_path):
    body = """
        print(json.dumps({"type": "turn.failed", "error": {"message": json.dumps({"status": 400})}}))
        sys.exit(1)
    """
    path, _ = fake_codex(tmp_path, body)
    with pytest.raises(ModelUnavailable) as caught:
        await CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={})
    assert (caught.value.code, caught.value.retryable, caught.value.upstream_status) == ("upstream_rejected", False, 400)


@pytest.mark.parametrize("status", [401, 402, 429])
async def test_login_quota_and_rate_limits_stay_retryable(tmp_path, status):
    body = f"""
        print(json.dumps({{"type": "turn.failed", "error": {{"message": json.dumps({{"status": {status}}})}}}}))
        sys.exit(1)
    """
    path, _ = fake_codex(tmp_path, body)
    with pytest.raises(ModelUnavailable) as caught:
        await CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={})
    assert caught.value.code == "upstream_unavailable" and caught.value.upstream_status == status


async def test_timeout_kills_the_process(tmp_path):
    path, _ = fake_codex(tmp_path, "import time\ntime.sleep(30)\n")
    started = time.monotonic()
    with pytest.raises(ModelUnavailable, match="did not finish"):
        await CodexCLIModel(executable=str(path), timeout=0.5).call(instruction="d", schema=SCHEMA, context={})
    assert time.monotonic() - started < 10


def launcher_body(pid_file):
    """A launcher like the npm wrapper: it starts the real program and waits, sharing its output pipe."""
    return f"""
        import subprocess, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        open({str(pid_file)!r}, "w").write(str(child.pid))
        child.wait()
    """


async def assert_process_stops(pid_file):
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as stat_file:
                if stat_file.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return
        except FileNotFoundError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("process is still running")


async def test_timeout_stops_what_the_launcher_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    path, _ = fake_codex(tmp_path, launcher_body(pid_file))
    started = time.monotonic()
    with pytest.raises(ModelUnavailable, match="did not finish"):
        await CodexCLIModel(executable=str(path), timeout=1).call(instruction="d", schema=SCHEMA, context={})
    assert time.monotonic() - started < 10
    await assert_process_stops(pid_file)


async def test_cancellation_stops_what_the_launcher_started(tmp_path):
    pid_file = tmp_path / "child.pid"
    path, _ = fake_codex(tmp_path, launcher_body(pid_file))
    task = asyncio.create_task(CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={}))
    await asyncio.sleep(1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await assert_process_stops(pid_file)


async def test_missing_executable_is_unavailable(tmp_path):
    with pytest.raises(ModelUnavailable, match="Could not start"):
        await CodexCLIModel(executable=str(tmp_path / "absent")).call(instruction="d", schema=SCHEMA, context={})


async def test_missing_output_file_is_a_rejected_response(tmp_path):
    path, _ = fake_codex(tmp_path, 'print(json.dumps({"type": "turn.completed"}))\n')
    with pytest.raises(ValueError):
        await CodexCLIModel(executable=str(path)).call(instruction="d", schema=SCHEMA, context={})
