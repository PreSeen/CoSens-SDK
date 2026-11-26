"""What the Python SDK ships to developers must not contain an internal name.

CoSens is the public product name. The check covers every file in the built sdist
and wheel (code, package metadata shown on PyPI, README and docs) and the repository
READMEs and examples published alongside the package. Tests and docs/testing records
are not shipped and are not checked.
"""

import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

INTERNAL_NAMES = re.compile("fri" "day|star" "dust", re.IGNORECASE)
PUBLIC_RSI_ENDPOINT = "https://friday-rsi-cn.stardust-ai.com/api"
PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_ROOT.parent


def _hits(name, data):
    text = data.decode("utf-8", errors="ignore").replace(PUBLIC_RSI_ENDPOINT, "")
    return [f"{name}: {m.group(0)}" for m in INTERNAL_NAMES.finditer(text)]


def test_built_distributions_have_no_internal_names(tmp_path):
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--sdist",
            "--wheel",
            "--outdir",
            str(tmp_path),
            str(PYTHON_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    hits = []
    wheels = list(tmp_path.glob("*.whl"))
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert wheels and sdists
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                hits += _hits(name, name.encode() + b"\n" + archive.read(name))
    for sdist in sdists:
        with tarfile.open(sdist) as archive:
            for member in archive.getmembers():
                if member.isfile():
                    data = archive.extractfile(member).read()
                    hits += _hits(member.name, member.name.encode() + b"\n" + data)
    assert hits == []


def test_published_readmes_and_examples_have_no_internal_names():
    files = [
        REPO_ROOT / "README.md",
        REPO_ROOT / "typescript" / "README.md",
        *PYTHON_ROOT.glob("*.md"),
        *(PYTHON_ROOT / "docs").glob("*.md"),
        *(PYTHON_ROOT / "examples").glob("*"),
        *(REPO_ROOT / "typescript" / "examples").glob("*"),
    ]
    hits = []
    for path in files:
        if path.is_file():
            hits += _hits(str(path.relative_to(REPO_ROOT)), path.read_bytes())
    assert hits == []
