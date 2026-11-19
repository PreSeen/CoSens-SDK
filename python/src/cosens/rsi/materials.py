"""Immutable local evidence snapshots; agents never edit host source files."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .models import MaterialManifest
from .store import uid


def digest(data):
    return hashlib.sha256(data).hexdigest()


def authorized_test_files(files, task):
    """Resolve an explicit test catalog against the attached Material snapshots."""
    if task is None:
        return []
    by_locator = {file["locator"]: file for file in files}
    selected = []
    for field, role in (
        ("input_locators", "input"),
        ("shared_reference_locators", "reference"),
    ):
        for locator in dict.fromkeys(task.get(field) or []):
            file = by_locator.get(locator)
            if file is None or file["role"] != role:
                raise ValueError(
                    f"{field} must name attached Material files with role {role}"
                )
            selected.append(file)
    return selected


def snapshot(root, manifest, config):
    manifest = MaterialManifest.model_validate(manifest)
    material_id = uid("material")
    directory = Path(root) / "materials" / material_id
    directory.mkdir(parents=True, mode=0o700)
    refs = [
        {
            "path": manifest.context_path,
            "role": "context",
            "description": "Complete host context",
        }
    ]
    refs.extend(f.model_dump() for f in manifest.files)
    files, total = [], 0
    try:
        for index, ref in enumerate(refs):
            source = Path(ref["path"]).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError("material paths must be regular files")
            with source.open("rb") as stream:
                data = stream.read(config.max_file_bytes + 1)
            if len(data) > config.max_file_bytes:
                raise ValueError(f"material file exceeds max_file_bytes: {source.name}")
            total += len(data)
            if total > config.max_bundle_bytes:
                raise ValueError("material bundle exceeds max_bundle_bytes")
            target = directory / (str(index) + source.suffix.lower())
            target.write_bytes(data)
            os.chmod(target, 0o400)
            files.append(
                {
                    **ref,
                    "path": str(source),
                    "locator": f"{material_id}/{index}",
                    "snapshot": str(target),
                    "sha256": digest(data),
                    "size": len(data),
                }
            )
        return {
            "id": material_id,
            "manifest": manifest.model_dump(),
            "files": files,
            "status": "ready",
            "name": Path(manifest.context_path).name,
        }
    except BaseException:
        shutil.rmtree(directory)
        raise


def read_snapshot(file, *, max_chars=100000):
    data = Path(file["snapshot"]).read_bytes()
    if digest(data) != file["sha256"]:
        raise ValueError("evidence snapshot integrity check failed")
    suffix = Path(file["path"]).suffix.lower()
    if suffix in {".docx", ".xlsx"}:
        # Extract text only, never evaluate formulas, macros or external links.
        with zipfile.ZipFile(file["snapshot"]) as archive:
            names = [
                n
                for n in archive.namelist()
                if (
                    n == "word/document.xml"
                    if suffix == ".docx"
                    else n == "xl/sharedStrings.xml"
                    or n.startswith("xl/worksheets/sheet")
                    and n.endswith(".xml")
                )
            ]
            if sum(archive.getinfo(n).file_size for n in names) > 16 * 1024 * 1024:
                raise ValueError("expanded document exceeds extraction limit")
            text = "\n".join(
                " ".join(ElementTree.fromstring(archive.read(n)).itertext())
                for n in sorted(names)
            )
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                "unsupported binary evidence; provide a host-generated text extraction"
            ) from None
        if "\x00" in text:
            raise ValueError("binary evidence requires a text extraction")
    return {
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "sha256": file["sha256"],
        "locator": file["locator"],
    }


def atomic_json(path, value):
    from .store import encode

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as stream:
        temporary = stream.name
        stream.write(encode(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
