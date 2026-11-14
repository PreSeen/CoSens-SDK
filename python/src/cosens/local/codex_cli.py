"""Run a Recipe stage through the user's installed Codex CLI and its logged-in quota."""

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from .models import ModelUnavailable, parse_object, positive_timeout, role_prompt


def _failure_status(message):
    """Codex reports provider errors as a JSON document inside the event message."""
    try:
        status = json.loads(message).get("status")
    except (ValueError, TypeError, AttributeError):
        return None
    return status if type(status) is int else None


def _turn_failure(stdout):
    """Return the message of a ``turn.failed`` event. ``error`` items can be mere config warnings."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.failed":
            error = event.get("error")
            return str(error.get("message", "")) if isinstance(error, dict) else ""
    return None


class CodexCLIModel:
    """Starts ``codex exec`` isolated from the user's hooks, MCP servers and working directory. Linux and macOS only."""

    def __init__(self, *, model=None, reasoning_effort="medium", timeout=300, executable="codex"):
        if sys.platform == "win32":
            raise RuntimeError("CodexCLIModel requires Linux or macOS")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError("model must be a nonempty model ID")
        if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
            raise ValueError("reasoning_effort must be nonempty")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._executable = executable
        self.timeout = positive_timeout(timeout)
        self.identity = "codex:" + (model or "default")

    def _command(self, workdir, schema_path, output_path):
        command = [self._executable, "exec", "--ephemeral", "--skip-git-repo-check", "--json", "-s", "read-only",
                   "-C", str(workdir), "--disable", "hooks", "-c", "mcp_servers={}",
                   "-c", f'model_reasoning_effort="{self._reasoning_effort}"', "-c", 'web_search="disabled"',
                   "--output-schema", str(schema_path), "-o", str(output_path)]
        if self._model:
            command += ["-m", self._model]
        return command + ["-"]

    async def call(self, *, instruction, schema, context):
        with tempfile.TemporaryDirectory(prefix="cosens-recipe-") as directory:
            root = Path(directory)
            workdir = root / "empty"
            workdir.mkdir()
            schema_path, output_path = root / "schema.json", root / "output.json"
            schema_path.write_text(json.dumps(schema))
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command(workdir, schema_path, output_path), cwd=workdir,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,  # its own process group, so a kill also reaches what the launcher starts
                )
            except OSError as exc:
                raise ModelUnavailable(f"Could not start {self._executable}.") from exc
            try:
                prompt = role_prompt(instruction, schema, context).encode()
                stdout, _ = await asyncio.wait_for(process.communicate(prompt), self.timeout)
            except asyncio.TimeoutError as exc:
                raise ModelUnavailable(f"Codex did not finish within {self.timeout} seconds.") from exc
            finally:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # it exited after the check above
                    await process.wait()
            failure = _turn_failure(stdout.decode(errors="replace"))
            if process.returncode or failure is not None:
                self._raise_failure(process.returncode, failure)
            return parse_object(output_path.read_text() if output_path.exists() else None)

    @staticmethod
    def _raise_failure(returncode, failure):
        status = _failure_status(failure) if failure else None
        if status is not None and status < 500 and status not in (401, 402, 403, 408, 429):  # login and quota can recover
            raise ModelUnavailable("Codex rejected the request.", code="upstream_rejected", retryable=False,
                                   upstream_status=status)
        raise ModelUnavailable(f"Codex did not complete (exit code {returncode}); check that it is logged in and has quota.",
                               upstream_status=status)
