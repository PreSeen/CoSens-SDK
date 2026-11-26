"""Run native Python and TypeScript scripts for Recipe replay."""

import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from .host_runtime import HostRuntimeUnavailable, host_command, host_environment
from .materials import digest

def _relative_path(path):
    if not path or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("invalid workspace-relative path")
    return PurePosixPath(path)


def _input_name(file):
    return f"{file['locator']}/{Path(file['path']).name}"


def host_input_path(file):
    return "../inputs/" + _input_name(file)


def _receipt(status, stderr, *, exit_code=None):
    return {"status": status, "exit_code": exit_code, "stdout": "", "stderr": stderr}


def _stage_file(target, data):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(data)
    target.chmod(0o600)


def _stage(workspace, files, staging, config):
    scripts, inputs, outputs = (
        staging / name for name in ("scripts", "inputs", "outputs")
    )
    for directory in (scripts, inputs, outputs):
        directory.mkdir(mode=0o700)
    size = count = 0
    for path in workspace.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("test workspace contains an unsupported file")
        if not path.is_file():
            continue
        relative = path.relative_to(workspace)
        count += 1
        length = path.stat().st_size
        size += length
        if count > 1000 or length > config.max_file_bytes or size > config.max_bundle_bytes:
            raise ValueError("test workspace exceeds the configured limits")
        destination = (
            scripts / Path(*relative.parts[1:])
            if relative.parts[0] == "scripts"
            else outputs / relative
        )
        _stage_file(destination, path.read_bytes())

    for file in files:
        if file["role"] not in {"input", "reference"}:
            raise ValueError("invalid replay evidence role")
        target = inputs / _relative_path(_input_name(file))
        data = Path(file["snapshot"]).read_bytes()
        if digest(data) != file["sha256"]:
            raise ValueError("evidence snapshot integrity check failed")
        _stage_file(target, data)

def _restore_output(workspace, output_directory, config, *, protected_files=()):
    if output_directory.is_symlink() or not output_directory.is_dir():
        raise ValueError("Host script replaced its output directory")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=workspace.parent) as temp:
        restored = Path(temp)
        size = count = 0
        for path in output_directory.rglob("*"):
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                continue
            relative = _relative_path(path.relative_to(output_directory).as_posix())
            if not stat.S_ISREG(info.st_mode) or relative.parts[0] == "scripts":
                raise ValueError("Host script produced an unsupported output file")
            count += 1
            if (
                count > 1000
                or info.st_size > config.max_file_bytes
                or size + info.st_size > config.max_bundle_bytes
            ):
                raise ValueError("Host script output exceeded the configured limits")
            with path.open("rb") as source:
                data = source.read(config.max_file_bytes + 1)
            size += len(data)
            if len(data) > config.max_file_bytes or size > config.max_bundle_bytes:
                raise ValueError("Host script output exceeded the configured limits")
            _stage_file(restored / relative, data)
        for name in protected_files:
            relative = _relative_path(name)
            if relative.parts[0] == "scripts":
                continue
            result = restored / relative
            if (
                not result.is_file()
                or result.read_bytes() != (workspace / relative).read_bytes()
            ):
                raise ValueError("Host script changed a Recipe package file")
        for path in workspace.iterdir():
            if path.name == "scripts":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in restored.iterdir():
            path.replace(workspace / path.name)


def _collect_host(command, cwd, check_alive):
    deadline = time.monotonic() + 30
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, subprocess.Popen(
        command,
        cwd=cwd,
        env=host_environment(),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=os.name == "posix",
    ) as child:
        try:
            while (code := child.poll()) is None:
                check_alive()
                if time.monotonic() >= deadline:
                    raise TimeoutError("Host script execution timed out")
                if any(os.fstat(stream.fileno()).st_size > 1024 * 1024 for stream in (stdout, stderr)):
                    raise ValueError("Host script log exceeded its size limit")
                time.sleep(0.05)
            check_alive()
        finally:
            if os.name == "posix":
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif child.poll() is None:
                child.kill()
        logs = []
        for stream in (stdout, stderr):
            stream.seek(0)
            logs.append(stream.read(17000).decode("utf-8", errors="replace"))
    return {"status": "completed", "exit_code": code, "stdout": logs[0], "stderr": logs[1]}


def run_host_script(path, args, workspace, files, config, check_alive, *, language, data_dir, protected_files=()):
    try:
        script = _relative_path(path)
        suffixes = {".py"} if language == "python" else {".ts", ".mts", ".cts"}
        if script.parts[0] != "scripts" or len(script.parts) < 2 or script.suffix not in suffixes:
            raise ValueError(f"Script path must name a {language} file under scripts/")
        if any("\0" in arg for arg in args):
            raise ValueError("Script arguments cannot contain NUL bytes")
        if not (workspace / script).is_file():
            raise ValueError("Script does not exist in the test workspace")
        with tempfile.TemporaryDirectory(prefix="rsi-host-") as temporary:
            directory = Path(temporary).resolve()
            _stage(workspace, files, directory, config)
            command = host_command(language, data_dir, check_alive)
            receipt = _collect_host(
                [*command, str(directory / script), *args], directory / "outputs", check_alive
            )
            if receipt["exit_code"] == 0:
                _restore_output(
                    workspace, directory / "outputs", config,
                    protected_files=protected_files,
                )
            return receipt
    except HostRuntimeUnavailable as error:
        return _receipt("unavailable", str(error))
    except (ValueError, TimeoutError) as error:
        return _receipt("rejected", str(error))
    except OSError:
        return _receipt("rejected", "Could not execute host script or collect its outputs")
