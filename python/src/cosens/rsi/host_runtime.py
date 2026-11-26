"""Locate trusted host interpreters for opt-in Recipe replay."""

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


_NODE_RELEASE = "v24.22.3"
_NODE_BASE_URL = f"https://nodejs.org/dist/{_NODE_RELEASE}/"
_MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
# Checksums from the pinned official Node distribution's SHASUMS256.txt.
_NODE_ASSETS = {
    ("Darwin", "arm64"): (
        "darwin-arm64", "ff6e17d4874b76d272212b908cc020d6e815fba5a6c7eaaa06add725840307fd",
    ),
    ("Darwin", "x64"): (
        "darwin-x64", "977614af0504c81045e7fd85596dbca0e5f951b96681516dadafedcca8c447f7",
    ),
    ("Linux", "arm64"): (
        "linux-arm64", "261af2af250dfd7e9f25cf151d1b2713a0adf8f10a9ad116c4947f68627affe8",
    ),
    ("Linux", "x64"): (
        "linux-x64", "061ec3e3062115705b56b51fa9cc330ab50d34b6969387d4e4b1a50c6809acc2",
    ),
    ("Windows", "arm64"): (
        "win-arm64", "172e320db179ce416fc012b96b9917dacfb442beb081de57c4deaed7b5a24644",
    ),
    ("Windows", "x64"): (
        "win-x64", "86fc4785327db5f5c7558dc6156c543b433c313e813a222e088086222ab4afa6",
    ),
}


class HostRuntimeUnavailable(Exception):
    pass


def host_environment():
    # Do not pass Worker credentials or language-runtime injection settings onward.
    allowed = {"PATH", "LANG", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed or name.upper().startswith("LC_")
    }


def _version(executable):
    try:
        result = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=host_environment(),
            timeout=5,
            check=False,
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _node_command(executable):
    version = re.fullmatch(r"v(\d+)\.(\d+)\.\d+", _version(executable))
    if version is None:
        return None
    major, minor = map(int, version.groups())
    if major >= 24 or (major == 23 and minor >= 6) or (major == 22 and minor >= 18):
        return [executable]
    if major == 23 or (major == 22 and minor >= 6):
        return [executable, "--experimental-strip-types"]
    return None


def _download_asset(asset, destination, sha256, check_alive):
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 300
    with urlopen(_NODE_BASE_URL + asset, timeout=15) as response, destination.open("wb") as output:
        while data := response.read(1024 * 1024):
            check_alive()
            size += len(data)
            if size > _MAX_DOWNLOAD_BYTES or time.monotonic() >= deadline:
                raise ValueError("Node.js download exceeded its limits")
            digest.update(data)
            output.write(data)
    if digest.hexdigest() != sha256:
        raise ValueError("Node.js download failed checksum verification")


def _managed_node(data_dir, check_alive):
    system = platform.system()
    machine = platform.machine().lower()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    selected = _NODE_ASSETS.get((system, arch))
    if selected is None:
        raise HostRuntimeUnavailable("No managed Node.js build for this host")
    target, sha256 = selected
    windows = system == "Windows"
    archive_name = f"node-{_NODE_RELEASE}-{target}"
    asset = f"{target}/node.exe" if windows else f"{archive_name}.tar.gz"
    directory = Path(data_dir) / "runtimes" / "node" / _NODE_RELEASE / target
    executable = directory / ("node.exe" if windows else "node")
    if executable.is_file() and (cached := _node_command(str(executable))) is not None:
        return cached
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="install-", dir=directory) as temporary:
            stage = Path(temporary)
            download = stage / executable.name if windows else stage / "node.tar.gz"
            _download_asset(asset, download, sha256, check_alive)
            candidate = stage / executable.name
            if not windows:
                with tarfile.open(download, "r:gz") as archive:
                    member = archive.getmember(f"{archive_name}/bin/node")
                    if not member.isfile() or not 0 < member.size <= _MAX_DOWNLOAD_BYTES:
                        raise ValueError("Invalid Node.js executable in archive")
                    with archive.extractfile(member) as source, candidate.open("wb") as output:
                        shutil.copyfileobj(source, output)
                candidate.chmod(0o700)
            check_alive()
            if _version(str(candidate)) != _NODE_RELEASE:
                raise ValueError("Downloaded Node.js executable failed validation")
            os.replace(candidate, executable)
    except (OSError, KeyError, ValueError, tarfile.TarError) as error:
        if executable.is_file() and (cached := _node_command(str(executable))) is not None:
            return cached
        raise HostRuntimeUnavailable("Could not provision the pinned Node.js runtime") from error
    return [str(executable)]


def host_command(language, data_dir, check_alive):
    if language == "python":
        if not sys.executable:
            raise HostRuntimeUnavailable("The SDK's Python interpreter is unavailable")
        return [sys.executable, "-I", "-B"]
    bun = shutil.which("bun")
    if bun and re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", _version(bun)):
        return [bun]
    node = shutil.which("node")
    if node and (command := _node_command(node)) is not None:
        return command
    return _managed_node(data_dir, check_alive)
