"""Install a verified cloud Recipe as a base for local learning."""

import hashlib
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import Conflict, InvalidState, NotFound, RSIError
from .recipe_file import read_recipe_file
from .recipe_identity import (
    BOOTSTRAP, IDENTITY, RECEIPT, journal_entry, recipe_id_for,
    require_unclaimed_upload_target, version_id_for,
)
from .recipe_package import (
    ENTRYPOINT, assert_recipe_package_matches, read_recipe_package, stage_recipe_package_files,
)

def _recipe_or_none(store, conn, recipe_id):
    try:
        return store.get_in(conn, recipe_id, "recipe")
    except NotFound:
        return None


def _version_path(store, recipe, version_id):
    storage_path = recipe["storage_path"]
    if not re.fullmatch(r"recipes/[A-Za-z0-9_-]+", storage_path) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", version_id
    ):
        raise InvalidState("invalid Recipe version storage path")
    directory = store.root / storage_path / version_id
    if any(part.is_symlink() for part in (directory.parent.parent, directory.parent, directory)):
        raise Conflict("unexpected Recipe version directory")
    return directory


def install_recipe_version_files(store, recipe, version_id, archive, *, staged=None):
    directory = _version_path(store, recipe, version_id)
    if directory.exists():
        assert_recipe_package_matches(directory, archive)
    else:
        directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if staged is not None:
            staged.rename(directory)
        else:
            with TemporaryDirectory(dir=directory.parent) as temporary:
                package = Path(temporary) / "version"
                stage_recipe_package_files(package, read_recipe_package(archive))
                package.rename(directory)
    return directory


def _pinned(store, conn, recipe, version_id):
    version = store.version_in(conn, recipe["id"], version_id)
    version_id = version["id"]
    cached = journal_entry(conn, BOOTSTRAP, version_id)
    status = conn.execute("SELECT status FROM versions WHERE id=?", (version_id,)).fetchone()[0]
    if status != "available" and not cached and not journal_entry(conn, RECEIPT, version_id):
        approved = status == "archived" and conn.execute(
            """SELECT 1 FROM executions
               WHERE recipe_id=? AND recipe_version_id=? AND status='completed'
                 AND json_extract(details,'$.result.test_status')='passed'
                 AND json_extract(details,'$.result.published')=1
                 AND json_extract(details,'$.result.provisional')=0 LIMIT 1""",
            (recipe["id"], version_id),
        ).fetchone()
        if not approved:
            raise InvalidState("base Recipe version is unavailable")
    path = _version_path(store, recipe, version_id) / ENTRYPOINT
    if path.is_symlink() or not path.is_file():
        raise InvalidState("base Recipe version has no local entrypoint")
    if cached and hashlib.sha256(path.read_bytes()).hexdigest() != cached[0]["entrypoint_sha256"]:
        raise Conflict("cached cloud Recipe entrypoint has changed")
    return {"version_id": version_id, "path": str(path)}


def _download_active(cloud, recipe_id, version_id):
    metadata = cloud.recipe_get(recipe_id, version=version_id or "active")
    if not isinstance(metadata, dict) or metadata.get("id") != recipe_id:
        raise RSIError("CoSens RSI returned a different Recipe")
    selected = metadata.get("selected_version")
    active = metadata.get("active_version_id")
    if metadata.get("archived_at") is not None or not isinstance(selected, dict) or not active:
        raise InvalidState("cloud Recipe has no available version")
    selected_id = selected.get("id")
    if selected_id != active or (version_id is not None and selected_id != version_id):
        raise Conflict("selected Recipe version is no longer active in CoSens RSI")
    sha256 = selected.get("package_sha256")
    if (
        selected.get("object_id") != recipe_id
        or not isinstance(selected_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selected_id)
        or selected.get("publication_state") != "published"
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[a-f0-9]{64}", sha256)
    ):
        raise InvalidState("cloud Recipe version has no verified published ZIP")
    archive = cloud.recipe_download(recipe_id, recipe_version_id=selected_id)
    if hashlib.sha256(archive).hexdigest() != sha256:
        raise Conflict("cloud Recipe ZIP SHA-256 does not match the selected version")
    return selected_id, archive, sha256


def _install(store, recipe_id, version_id, archive, sha256, destination):
    files = read_recipe_package(archive)
    with TemporaryDirectory(prefix="recipe-bootstrap-", dir=store.root) as temporary:
        staged = Path(temporary) / "version"
        staged.mkdir(mode=0o700)
        for name, contents in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(contents)
        content, markdown = read_recipe_file(staged / ENTRYPOINT)
        with store.transaction() as conn:
            if recipe_id_for(conn, recipe_id) != recipe_id or version_id_for(conn, version_id) != version_id:
                raise Conflict("cloud Recipe or version is already linked to a different local ID")
            recipe = _recipe_or_none(store, conn, recipe_id)
            identity = journal_entry(conn, IDENTITY, recipe_id)
            if recipe is not None and (not identity or identity[0] != destination):
                raise Conflict("local Recipe is already linked to a different identity")
            if recipe is not None and recipe["archived_at"] is not None:
                raise InvalidState("archived recipe cannot be used")
            existing = conn.execute("SELECT recipe_id FROM versions WHERE id=?", (version_id,)).fetchone()
            if existing is not None:
                cached = journal_entry(conn, BOOTSTRAP, version_id) or journal_entry(conn, RECEIPT, version_id)
                if existing["recipe_id"] != recipe_id or not cached or cached[0].get("zip_sha256") != sha256:
                    raise Conflict("cloud Recipe version conflicts with a local version")
                install_recipe_version_files(store, recipe, version_id, archive, staged=staged)
                return _pinned(store, conn, recipe, version_id)
            require_unclaimed_upload_target(conn, recipe_id)
            if recipe is None:
                recipe = store.insert(conn, "recipe", {"id": recipe_id, "name": content["name"], "active_version_id": None})
                store.remember(conn, IDENTITY, recipe_id, destination, {})
            install_recipe_version_files(store, recipe, version_id, archive, staged=staged)
            store.append_version(
                conn, recipe, content, recipe_markdown=markdown, version_id=version_id,
                provenance={"source": "cloud", "package_sha256": sha256},
            )
            if not recipe["active_version_id"]:
                store.set_recipe_version_status(conn, recipe, version_id, "available")
            store.remember(conn, BOOTSTRAP, version_id, {
                "zip_sha256": sha256,
                "entrypoint_sha256": hashlib.sha256(files[ENTRYPOINT]).hexdigest(),
            }, {})
            return _pinned(store, conn, recipe, version_id)


def resolve_base(store, base, cloud_factory):
    """Keep a usable local base offline and resolve a missing official base once."""
    with store.connect() as conn:
        conn.execute("BEGIN")
        recipe_id = recipe_id_for(conn, base["id"])
        requested = version_id_for(conn, base.get("version_id"))
        recipe = _recipe_or_none(store, conn, recipe_id)
        if recipe is None and conn.execute(
            """SELECT id FROM executions WHERE id=?
               UNION ALL SELECT id FROM evidence_snapshots WHERE id=?
               UNION ALL SELECT rubrics_id FROM versions WHERE rubrics_id=? LIMIT 1""",
            (recipe_id, recipe_id, recipe_id),
        ).fetchone():
            raise NotFound(f"recipe not found: {recipe_id}")
        if recipe is not None:
            if recipe["archived_at"] is not None:
                raise InvalidState("archived recipe cannot be used")
            selected = requested or recipe["active_version_id"]
            if not selected:
                raise InvalidState("base Recipe has no available version")
            record = conn.execute(
                "SELECT recipe_id,json_extract(data,'$.provenance.source') AS source FROM versions WHERE id=?",
                (selected,),
            ).fetchone()
            if record is not None:
                if record["recipe_id"] != recipe_id:
                    raise Conflict("selected version belongs to a different local Recipe")
                if requested or record["source"] == "cloud" or journal_entry(conn, BOOTSTRAP, selected):
                    return _pinned(store, conn, recipe, selected)
                # Unpinned earlier calls on native local Recipes use the caller's Markdown.
                return None
            if not journal_entry(conn, IDENTITY, recipe_id):
                raise NotFound("Recipe version not found")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", recipe_id) or (
        requested is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", requested)
    ):
        raise ValueError("invalid cloud Recipe or version ID")
    cloud = cloud_factory()
    try:
        destination = cloud._recipe_upload_destination()
        version_id, archive, sha256 = _download_active(cloud, recipe_id, requested)
    finally:
        cloud.close()
    return _install(store, recipe_id, version_id, archive, sha256, destination)
