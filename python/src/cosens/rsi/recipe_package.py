"""Build and verify the immutable ZIP for a local Recipe version."""

from __future__ import annotations

import io
import json
import os
import stat
import unicodedata
import zlib
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile, ZipInfo

from .recipe_file import read_recipe_file

ENTRYPOINT = "RECIPE.md"
MANIFEST = "manifest.json"
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 256


class _PackagePaths:
    def __init__(self):
        self.entries: dict[str, bool] = {}

    def add(self, name: str, directory: bool) -> None:
        parts = name.split("/")
        if (
            any(part in {"", ".", ".."} for part in parts)
            or any(
                char in '\\:*?"<>|' or ord(char) < 32 or ord(char) == 127
                for char in name
            )
        ):
            raise ValueError("Recipe ZIP paths must be relative POSIX paths")
        key = unicodedata.normalize("NFC", name).casefold() + "/"
        if key in self.entries:
            raise ValueError("Recipe ZIP has duplicate or conflicting paths")
        self.entries[key] = directory

    def check_conflicts(self) -> None:
        paths = sorted(self.entries)
        for previous, current in zip(paths, paths[1:]):
            if not self.entries[previous] and current.startswith(previous):
                raise ValueError("Recipe ZIP has duplicate or conflicting paths")


def _check_size(size: int, total: int) -> None:
    if size > MAX_FILE_BYTES:
        raise ValueError(f"Recipe ZIP file exceeds {MAX_FILE_BYTES} bytes")
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"Recipe ZIP unpacked contents exceed {MAX_UNCOMPRESSED_BYTES} bytes")


def _read_zip_files(archive: ZipFile) -> dict[str, bytes]:
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES:
        raise ValueError(f"Recipe ZIP exceeds {MAX_ENTRIES} entries")
    paths = _PackagePaths()
    total = 0
    files = {}
    for entry in entries:
        directory = entry.is_dir()
        name = entry.orig_filename
        paths.add(name.removesuffix("/") if directory else name, directory)
        kind = stat.S_IFMT(entry.external_attr >> 16)
        expected_kind = stat.S_IFDIR if directory else stat.S_IFREG
        if kind not in {0, expected_kind} or (directory and entry.file_size):
            raise ValueError("Recipe ZIP entries must be regular files or directories")
        if entry.flag_bits & 0x41:
            raise ValueError("Recipe ZIP cannot contain encrypted entries")
        if entry.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
            raise ValueError("Recipe ZIP must use stored or deflated compression")
        total += entry.file_size
        _check_size(entry.file_size, total)
        if not directory:
            content = archive.read(entry)
            if len(content) != entry.file_size:
                raise ValueError("Recipe ZIP entry has an invalid size")
            files[entry.filename] = content
    paths.check_conflicts()
    return files


def _unique_manifest_keys(pairs):
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError("duplicate manifest keys")
    return data


def _validate_entrypoint(content: bytes) -> None:
    # The existing Markdown reader applies the same frontmatter rules as the service.
    with TemporaryDirectory(prefix="cosens-recipe-") as directory:
        path = Path(directory) / ENTRYPOINT
        path.write_bytes(content)
        metadata, _ = read_recipe_file(path)
    if len(metadata["name"]) > 160 or len(metadata["description"]) > 8000:
        raise ValueError("Recipe name must be at most 160 characters and description at most 8000")


def read_recipe_package(data: bytes) -> dict[str, bytes]:
    """Return verified payload files with their original paths and bytes."""
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"Recipe ZIP exceeds {MAX_ZIP_BYTES} bytes")
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            files = _read_zip_files(archive)
    except (BadZipFile, EOFError, RuntimeError, NotImplementedError, UnicodeError, zlib.error):
        raise ValueError("Recipe ZIP could not be read") from None

    if ENTRYPOINT not in files or MANIFEST not in files:
        raise ValueError(f"Recipe ZIP requires {ENTRYPOINT} and {MANIFEST} at its root")
    try:
        manifest = json.loads(
            files.pop(MANIFEST).decode("utf-8"), object_pairs_hook=_unique_manifest_keys
        )
    except (ValueError, RecursionError):
        raise ValueError(f"{MANIFEST} must be a valid UTF-8 JSON object") from None
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"entrypoint", "files"}
        or manifest["entrypoint"] != ENTRYPOINT
        or not isinstance(manifest["files"], list)
        or not all(isinstance(path, str) for path in manifest["files"])
        or sorted(manifest["files"]) != sorted(files)
    ):
        raise ValueError(f"{MANIFEST} must list the entrypoint and every Recipe file exactly once")
    _validate_entrypoint(files[ENTRYPOINT])
    return files


def _read_regular_file(path: Path, info: os.stat_result, total: int = 0) -> bytes:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Recipe version files must be regular files")
    _check_size(info.st_size, total + info.st_size)
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (
            info.st_dev, info.st_ino
        ) != (opened.st_dev, opened.st_ino):
            raise ValueError("Recipe version file changed while packaging")
        _check_size(opened.st_size, total + opened.st_size)
        content = stream.read(opened.st_size + 1)
        if (
            len(content) != opened.st_size
            or os.fstat(stream.fileno()).st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError("Recipe version file changed while packaging")
    return content


def _read_directory(directory: Path) -> dict[str, bytes]:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ValueError("Recipe version path must be a regular directory")
    paths = _PackagePaths()
    paths.add(MANIFEST, False)
    remaining = [directory]
    total = 0
    files = {}
    while remaining:
        for path in remaining.pop().iterdir():
            relative = path.relative_to(directory).as_posix()
            info = path.lstat()
            is_directory = stat.S_ISDIR(info.st_mode)
            paths.add(relative, is_directory)
            if is_directory:
                remaining.append(path)
                continue
            if len(files) + 2 > MAX_ENTRIES:
                raise ValueError(f"Recipe ZIP exceeds {MAX_ENTRIES} entries")
            content = _read_regular_file(path, info, total)
            total += len(content)
            files[relative] = content
    paths.check_conflicts()
    if ENTRYPOINT not in files:
        raise ValueError(f"Recipe version requires {ENTRYPOINT} at its root")
    return files


def read_recipe_version_resources(directory: Path) -> dict[str, bytes]:
    files = _read_directory(directory)
    del files[ENTRYPOINT]
    return files


def describe_recipe_resources(files: dict[str, bytes]) -> list[dict]:
    remaining = 32 * 1024
    resources = []
    for path, data in sorted(files.items()):
        info = {"path": path, "size": len(data)}
        if len(data) <= min(8 * 1024, remaining) and b"\0" not in data:
            try:
                info["text"] = data.decode("utf-8")
                remaining -= len(data)
            except UnicodeDecodeError:
                pass
        resources.append(info)
    return resources


def stage_recipe_package_files(directory: Path, files: dict[str, bytes]) -> None:
    if not files:
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ValueError("Recipe package destination must be a regular directory")
    paths = _PackagePaths()
    for name in files:
        paths.add(name, False)
    paths.check_conflicts()
    for name, data in files.items():
        target = directory / name
        parent = directory
        for part in name.split("/")[:-1]:
            parent = parent / part
            parent.mkdir(mode=0o700, exist_ok=True)
            if not stat.S_ISDIR(parent.lstat().st_mode):
                raise ValueError("Recipe package destination cannot contain a symlink")
        try:
            info = target.lstat()
        except FileNotFoundError:
            temporary = None
            try:
                with NamedTemporaryFile(dir=directory.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o400)
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            info = target.lstat()
        if _read_regular_file(target, info) != data:
            raise ValueError("Recipe package destination already has different contents")


def build_recipe_package(directory: Path) -> bytes:
    """Build a deterministic ZIP from a version directory and its resources."""
    files = _read_directory(directory)
    _validate_entrypoint(files[ENTRYPOINT])
    files[MANIFEST] = json.dumps(
        {"entrypoint": ENTRYPOINT, "files": sorted(files)}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    _check_size(len(files[MANIFEST]), sum(map(len, files.values())))
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for path, content in sorted(files.items()):
            entry = ZipInfo(path)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, content, compress_type=ZIP_DEFLATED, compresslevel=6)
    data = output.getvalue()
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError(f"Recipe ZIP exceeds {MAX_ZIP_BYTES} bytes")
    return data


def assert_recipe_package_matches(directory: Path, data: bytes) -> None:
    """Require the ZIP to contain exactly the selected version's files."""
    if read_recipe_package(data) != _read_directory(directory):
        raise ValueError("Recipe ZIP does not match the selected local Recipe version")
