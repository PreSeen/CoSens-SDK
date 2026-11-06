"""What the Python SDK ships to developers must follow two public-surface rules.

1. Nothing may contain an internal name ("Friday" or "Stardust", including hostnames
   and emails such as stardust-ai.com). CoSens is the public product name.
   These checks cover the built sdist and wheel, the package metadata shown on PyPI,
   the public names of the ``cosens`` package, the HTTP requests its clients send, and
   the repository docs and examples that are published alongside the package.
2. The SDK ships no provider-specific model client factory. Developers build the
   standard ``openai.OpenAI(base_url=..., api_key=...)`` (or ``AsyncOpenAI``) for
   whichever provider they use and pass it to ``cosens(...)``. These checks cover the
   public names of the package, the shipped files, and the published READMEs and
   examples.

Not checked (not shipped to developers): the tests directory (including this file),
``docs/testing/`` validation records, and editor/CI/dev tooling files.
"""
import hashlib
import io
import re
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

import httpx
import pytest

import cosens

# Internal names that must not reach developers, matched case-insensitively anywhere
# (identifiers, hostnames, emails, prose).
INTERNAL_NAMES = re.compile("friday|stardust", re.IGNORECASE)
PYTHON_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PYTHON_ROOT.parent

# Public names that would expose a model client factory, and shipped files that would
# hold a provider gateway module.
MODEL_CLIENT_FACTORY_NAME = re.compile(r"gateway.?client|model.?client", re.IGNORECASE)
GATEWAY_FILE_NAME = re.compile("gateway", re.IGNORECASE)

# Repository docs published next to the package: READMEs, usage docs, examples and
# the .env template developers copy.
PUBLISHED_DOCS = [
    REPO_ROOT / "README.md",
    PYTHON_ROOT / "README.md",
    PYTHON_ROOT / ".env.example",
    *sorted((PYTHON_ROOT / "docs").glob("*.md")),
    *sorted((PYTHON_ROOT / "examples").rglob("*.py")),
]

# Published READMEs and examples that show how a model client is wrapped.
CLIENT_DOCS = [
    REPO_ROOT / "README.md",
    PYTHON_ROOT / "README.md",
    *sorted((PYTHON_ROOT / "examples").rglob("*.py")),
]
# A model client is wrapped by passing it to cosens(...) or installing it as the
# OpenAI Agents default client.
WRAPS_MODEL_CLIENT = re.compile(r"(?<![\w.])(?:cosens|set_default_openai_client)\(")
STANDARD_CONSTRUCTOR = re.compile(r"(?<!\w)(?:Async)?OpenAI\(")
FACTORY_CALL = re.compile(r"\w*gateway_?client\s*\(", re.IGNORECASE)

# Generated metadata is checked by test_package_metadata; RECORD only lists member
# paths, which are checked as paths.
_GENERATED_METADATA = re.compile(r"(^|/)(PKG-INFO|METADATA|RECORD)$")


def _matched(*texts):
    """Return the internal names found in ``texts`` as a tag such as "[friday, stardust]"."""
    names = sorted({match.lower() for text in texts for match in INTERNAL_NAMES.findall(text)})
    return f"[{', '.join(names)}]" if names else ""


def _text_matches(label, data):
    text = data.decode("utf-8", errors="replace")
    return [f"{label}:{number}: {_matched(line)} {line.strip()}" for number, line in enumerate(text.splitlines(), 1)
            if _matched(line)]


def _report(findings, what):
    return f"{len(findings)} occurrence(s) of an internal name in {what}:\n" + "\n".join(findings)


@pytest.fixture(scope="module")
def built_distributions(tmp_path_factory):
    """Build the sdist and wheel exactly as the packaging config defines them."""
    from build import ProjectBuilder
    out = tmp_path_factory.mktemp("dist")
    builder = ProjectBuilder(PYTHON_ROOT)
    sdist = Path(builder.build("sdist", out))
    wheel = Path(builder.build("wheel", out))
    with tarfile.open(sdist) as archive:
        sdist_members = {m.name.split("/", 1)[1]: archive.extractfile(m).read()
                         for m in archive.getmembers() if m.isfile() and "/" in m.name}
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
    assert "src/cosens/__init__.py" in sdist_members and "cosens/__init__.py" in wheel_members
    return {"sdist": sdist_members, "wheel": wheel_members}


def test_packaged_files_do_not_contain_internal_names(built_distributions):
    findings, seen = [], set()
    for kind, members in built_distributions.items():
        for path, data in sorted(members.items()):
            if _matched(path):
                findings.append(f"{kind}:{path}: {_matched(path)} file path")
            digest = hashlib.sha256(data).digest()
            if _GENERATED_METADATA.search(path) or digest in seen:
                continue
            seen.add(digest)
            findings += _text_matches(f"{kind}:{path}", data)
    assert not findings, _report(findings, "the built sdist/wheel")


def test_package_metadata_does_not_contain_internal_names(built_distributions):
    name = next(path for path in built_distributions["wheel"] if path.endswith(".dist-info/METADATA"))
    raw = built_distributions["wheel"][name]
    metadata = Parser().parsestr(raw.decode("utf-8"))
    # Every header field: Name, Summary, Author, Author-email, Maintainer(-email),
    # Home-page, Project-URL, License, Keywords, and so on.
    findings = [f"METADATA {field}: {_matched(value)} {value}" for field, value in metadata.items() if _matched(value)]
    findings += _text_matches("METADATA description (README.md)", metadata.get_payload().encode())
    assert not findings, _report(findings, "the wheel metadata")


def _public_names():
    return sorted(set(dir(cosens)) | set(getattr(cosens, "__all__", ())))


def test_public_exports_do_not_contain_internal_names():
    findings = [f"cosens.{name} {_matched(name)}" for name in _public_names() if _matched(name)]
    assert not findings, _report(findings, "public names of the cosens package")


def test_public_exports_have_no_model_client_factory():
    findings = [f"cosens.{name}" for name in _public_names() if MODEL_CLIENT_FACTORY_NAME.search(name)]
    assert not findings, (f"{len(findings)} model client factory name(s) exported by the cosens package; "
                          "developers construct openai.OpenAI(base_url=..., api_key=...) themselves:\n"
                          + "\n".join(findings))


def test_packaged_files_have_no_gateway_module(built_distributions):
    findings = [f"{kind}:{path}" for kind, members in built_distributions.items()
                for path in sorted(members) if GATEWAY_FILE_NAME.search(path.rsplit("/", 1)[-1])]
    assert not findings, (f"{len(findings)} shipped file(s) named *gateway* in the built sdist/wheel; "
                          "the SDK ships no provider gateway module:\n" + "\n".join(findings))


def _request_findings(label, requests):
    findings = []
    for request in requests:
        if _matched(str(request.url)):
            findings.append(f"{label}: URL {_matched(str(request.url))} {request.url}")
        for header, value in request.headers.items():
            if header.lower() == "host":  # derived from the URL checked above
                continue
            if _matched(header, value):
                findings.append(f"{label}: header {_matched(header, value)} {header}: {value}")
    return findings


def _record(seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [], "task_id": "t"})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("region", ["cn", "us"])
def test_http_requests_do_not_contain_internal_names(monkeypatch, region):
    monkeypatch.setenv("COSENS_REGION", region)
    api_requests = []
    client = cosens.CoSensClient(_http_transport=_record(api_requests))
    client.sync_atoms.get_task("t")
    assert api_requests, "CoSensClient sent no request"
    findings = _request_findings(f"{region} CoSensClient (default base URL)", api_requests)

    assert not findings, _report(findings, "HTTP requests sent by the SDK")


def test_published_docs_do_not_contain_internal_names():
    findings = []
    for path in PUBLISHED_DOCS:
        label = path.relative_to(REPO_ROOT).as_posix()
        if _matched(label):
            findings.append(f"{label}: {_matched(label)} file path")
        findings += _text_matches(label, path.read_bytes())
    assert not findings, _report(findings, "published repository docs and examples")


def _call_arguments(text, open_paren):
    """Return the text between the parenthesis at ``open_paren`` and its match."""
    depth = 0
    for index in range(open_paren, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return text[open_paren + 1:index]
    return text[open_paren + 1:]


def _builds_standard_client(text):
    for match in STANDARD_CONSTRUCTOR.finditer(text):
        arguments = _call_arguments(text, match.end() - 1)
        if re.search(r"\bbase_url\s*=", arguments) and re.search(r"\bapi_key\s*=", arguments):
            return True
    return False


def test_published_docs_build_model_clients_with_standard_openai_constructor():
    findings = []
    for path in CLIENT_DOCS:
        label = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        lines = list(enumerate(text.splitlines(), 1))
        findings += [f"{label}:{number}: calls a model client factory: {line.strip()}"
                     for number, line in lines if FACTORY_CALL.search(line)]
        if not _builds_standard_client(text):
            findings += [f"{label}:{number}: wraps a model client, but the file never builds "
                         f"OpenAI(base_url=..., api_key=...): {line.strip()}"
                         for number, line in lines if WRAPS_MODEL_CLIENT.search(line)]
    assert not findings, (f"{len(findings)} published README/example line(s) do not build the model client "
                          "with the standard OpenAI constructor:\n" + "\n".join(findings))
