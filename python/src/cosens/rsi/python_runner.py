"""Bounded, container-based Python execution for Recipe tests."""

import base64
import binascii
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .materials import digest

# The interpreter that runs the untrusted script receives no Docker socket,
# host credentials, writable host mount, or access to the host PID namespace.
SUPERVISOR = r"""
import base64, json, os, shutil, signal, stat, subprocess, sys, tempfile
from pathlib import Path

workspace = Path('/workspace')
output = workspace / 'outputs'
max_file, max_bundle = map(int, sys.argv[1:3])

def logs(stream):
    stream.seek(0)
    data = stream.read(16385)
    return data[:16384].decode('utf-8', errors='replace') + ('\n[truncated]' if len(data) > 16384 else '')

def artifacts():
    files, total = {}, 0
    for directory, folders, names in os.walk(output):
        for folder in folders:
            if (Path(directory) / folder).is_symlink():
                raise ValueError('output symlinks are unsupported')
        for name in names:
            file = Path(directory) / name
            path = file.relative_to(output).as_posix()
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or '\\' in path or path.split('/')[0] == 'scripts':
                raise ValueError('unsafe output file')
            total += info.st_size
            if info.st_size > max_file or total > max_bundle or len(files) >= 1000:
                raise ValueError('output exceeds the configured limits')
            files[path] = base64.b64encode(file.read_bytes()).decode('ascii')
    return files

receipt = {'exit_code': None, 'stdout': '', 'stderr': '', 'outputs': {}}
try:
    for item in (workspace / 'seed').iterdir():
        target = output / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copyfile(item, target)
    for directory, folders, names in os.walk(output):
        for name in folders + names:
            os.chown(Path(directory) / name, 65534, 65534)
    with tempfile.TemporaryFile(dir='/tmp') as stdout, tempfile.TemporaryFile(dir='/tmp') as stderr:
        try:
            process = subprocess.run([sys.executable, '-I', '-B', str(workspace / sys.argv[3]), *sys.argv[4:]], cwd=output, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=30, check=False, user=65534, group=65534, extra_groups=())
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        # Stop any descendant still writing to the isolated output mount.
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        receipt.update(exit_code=exit_code, stdout=logs(stdout), stderr=logs(stderr))
        if exit_code == 124:
            receipt['stderr'] += '\nPython execution timed out'
    if receipt['exit_code'] == 0:
        receipt['outputs'] = artifacts()
except (OSError, ValueError) as error:
    receipt.update(exit_code=None, stderr=str(error), outputs={})
print(json.dumps(receipt))
"""


def _relative_path(path):
    if not path or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("invalid workspace-relative path")
    return PurePosixPath(path)


def sandbox_path(file):
    return f"/workspace/inputs/{file['locator']}/{Path(file['path']).name}"


def _receipt(status, stderr, *, exit_code=None):
    return {"status": status, "exit_code": exit_code, "stdout": "", "stderr": stderr}


def _stage_file(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o644)


def _stage(workspace, files, staging, config):
    scripts, inputs, seed = (staging / name for name in ("scripts", "inputs", "seed"))
    for directory in (scripts, inputs, seed):
        directory.mkdir()
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
        destination = scripts / Path(*relative.parts[1:]) if relative.parts[0] == "scripts" else seed / relative
        _stage_file(destination, path.read_bytes())

    for file in files:
        if file["role"] not in {"input", "reference"}:
            raise ValueError("invalid sandbox evidence role")
        prefix = "/workspace/inputs/"
        target = inputs / _relative_path(sandbox_path(file)[len(prefix):])
        data = Path(file["snapshot"]).read_bytes()
        if digest(data) != file["sha256"]:
            raise ValueError("evidence snapshot integrity check failed")
        _stage_file(target, data)

    # Docker runs the interpreter as an unprivileged UID. The daemon mounts
    # only these child directories, each read-only inside the sandbox.
    for path in staging.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)


def _collect(command, budget, check_alive):
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 45
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child, selectors.DefaultSelector() as selector:
        try:
            selector.register(child.stdout, selectors.EVENT_READ, "stdout")
            selector.register(child.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                check_alive()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Python sandbox timed out")
                for event, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(event.fd, 65536)
                    if not data:
                        selector.unregister(event.fileobj)
                        continue
                    stream = streams[event.data]
                    limit = budget if event.data == "stdout" else 64 * 1024
                    if len(stream) + len(data) > limit:
                        raise ValueError("Python sandbox receipt exceeded its size limit")
                    stream.extend(data)
            return child.wait(timeout=max(0, deadline - time.monotonic())), bytes(streams["stdout"])
        finally:
            if child.poll() is None:
                child.kill()


def _docker_command(name, image, staging, config, script, args):
    command = [
        "docker", "run", "--rm", "--name", name, "--pull=never", "--log-driver=none", "--init=false",
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--cap-add=SETUID", "--cap-add=SETGID", "--cap-add=KILL", "--cap-add=CHOWN",
        "--cap-add=DAC_OVERRIDE", "--user=0:0", "--pids-limit=64", "--cpus=1",
        "--memory=512m", "--memory-swap=512m",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "--tmpfs", f"/workspace/outputs:rw,noexec,nosuid,nodev,size={config.max_bundle_bytes},nr_inodes=4096,mode=1777",
        "--ulimit", f"fsize={config.max_file_bytes}:{config.max_file_bytes}",
    ]
    for directory in ("scripts", "inputs", "seed"):
        source = staging / directory
        if "," in str(source):
            raise ValueError("sandbox staging paths cannot contain commas")
        command.extend(("--mount", f"type=bind,source={source},target=/workspace/{directory},readonly"))
    return command + [
        "--entrypoint=python3", image, "-I", "-B", "-c", SUPERVISOR,
        str(config.max_file_bytes), str(config.max_bundle_bytes), script, *args,
    ]


def _installed_image(image):
    try:
        inspected = subprocess.run(
            [
                "docker", "image", "inspect", "--format",
                '{{printf "[%q,%s]" .Id (json (index .Config "Volumes"))}}', image,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
        if inspected.returncode == 0:
            image_id, volumes = json.loads(inspected.stdout)
            if re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) and volumes in (None, {}):
                return image_id
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        pass
    return None


def _restore_output(workspace, payload, config):
    if not isinstance(payload, dict) or len(payload) > 1000:
        raise ValueError("invalid sandbox output manifest")
    size = 0
    with tempfile.TemporaryDirectory(prefix="restore-", dir=workspace.parent) as temp:
        restored = Path(temp)
        for name, encoded in payload.items():
            relative = _relative_path(name)
            if relative.parts[0] == "scripts" or not isinstance(encoded, str):
                raise ValueError("invalid sandbox output file")
            data = base64.b64decode(encoded, validate=True)
            size += len(data)
            if len(data) > config.max_file_bytes or size > config.max_bundle_bytes:
                raise ValueError("sandbox output exceeded the configured limits")
            target = restored / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
            target.chmod(0o600)
        for path in workspace.iterdir():
            if path.name == "scripts":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in restored.iterdir():
            path.replace(workspace / path.name)


def run_python(path, args, workspace, files, config, check_alive):
    try:
        script = _relative_path(path)
        if script.parts[0] != "scripts" or script.suffix != ".py" or len(script.parts) < 2:
            raise ValueError("script path must name a Python file under scripts/")
        if any("\0" in arg for arg in args):
            raise ValueError("script arguments cannot contain NUL bytes")
        if not (workspace / script).is_file():
            raise ValueError("Python script does not exist in the test workspace")
        if not config.test_python_image:
            return _receipt("unavailable", "Python runner is not configured (test_python_image)")
        if not shutil.which("docker"):
            return _receipt("unavailable", "Docker is unavailable for Python tests")
        image = _installed_image(config.test_python_image)
        if image is None:
            return _receipt("unavailable", "Docker Python image must be installed locally without declared volumes")
        with tempfile.TemporaryDirectory(prefix="rsi-python-") as temporary:
            directory = Path(temporary).resolve()
            _stage(workspace, files, directory, config)
            if not (directory / script).is_file():
                raise ValueError("Python script does not exist in the test workspace")
            container = "rsi-python-" + uuid4().hex
            command = _docker_command(container, image, directory, config, str(script), args)
            try:
                code, response = _collect(command, 2 * config.max_bundle_bytes + 1024 * 1024, check_alive)
            finally:
                # The Docker client may be interrupted while its detached daemon still owns the container.
                try:
                    subprocess.run(["docker", "rm", "-f", container], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if code != 0:
                return _receipt("unavailable", "Docker could not run the configured Python image")
            receipt = json.loads(response)
            if (
                not isinstance(receipt, dict)
                or "exit_code" not in receipt
                or (receipt["exit_code"] is not None and type(receipt["exit_code"]) is not int)
                or not isinstance(receipt.get("stdout"), str)
                or not isinstance(receipt.get("stderr"), str)
            ):
                raise ValueError("invalid Python sandbox receipt")
            if receipt["exit_code"] == 0:
                check_alive()
                _restore_output(workspace, receipt.get("outputs"), config)
            return {
                "status": "rejected" if receipt["exit_code"] is None else "completed",
                "exit_code": receipt["exit_code"],
                "stdout": receipt["stdout"][:17000],
                "stderr": receipt["stderr"][:17000],
            }
    except (TimeoutError, subprocess.TimeoutExpired):
        return _receipt("rejected", "Python sandbox timed out before returning an execution receipt")
    except OSError:
        return _receipt("rejected", "Could not stage Python test files or collect sandbox output")
    except (ValueError, binascii.Error) as error:
        return _receipt("rejected", str(error))
