"""Adopt the Recipe service's formal identifiers in the local SQLite store."""

import json
import re
import shutil
import tempfile
from pathlib import Path

from .models import Conflict, InvalidState
from .recipe_file import render_recipe_markdown

IDENTITY = "recipe_cloud_identity"
RECIPE_ALIAS = "recipe_cloud_id_alias"
VERSION_ALIAS = "recipe_cloud_version_alias"
PENDING = "recipe_cloud_upload_pending"
RECEIPT = "recipe_cloud_upload_receipt"
DESTINATION_FIELDS = ("base_url", "virtual_user_id")


def journal_entry(conn, scope, key):
    row = conn.execute(
        "SELECT request,result FROM idempotency WHERE scope=? AND key=?", (scope, key)
    ).fetchone()
    return (json.loads(row["request"]), json.loads(row["result"])) if row else None


def recipe_id_for(conn, recipe_id):
    entry = journal_entry(conn, RECIPE_ALIAS, recipe_id) if recipe_id else None
    return entry[1]["id"] if entry else recipe_id


def version_id_for(conn, version_id):
    entry = journal_entry(conn, VERSION_ALIAS, version_id) if version_id else None
    return entry[1]["id"] if entry else version_id


_EXECUTION_REFERENCES = """recipe_id=? OR recipe_version_id=? OR base_recipe_version_id=?
    OR json_extract(details,'$.publication_base_version_id')=?
    OR json_extract(details,'$.test_request.recipe_id')=?
    OR json_extract(details,'$.test_request.version_id')=?
    OR EXISTS (
        SELECT 1 FROM json_each(details,'$.test_results')
        WHERE json_extract(value,'$.recipe_id')=? OR json_extract(value,'$.version_id')=?
    )"""


def require_idle_recipe(conn, recipe_id, version_id):
    if conn.execute(
        f"""SELECT 1 FROM executions WHERE status IN ('queued','running')
        AND ({_EXECUTION_REFERENCES}) LIMIT 1""",
        (
            recipe_id,
            version_id,
            version_id,
            version_id,
            recipe_id,
            version_id,
            recipe_id,
            version_id,
        ),
    ).fetchone():
        raise Conflict(
            "finish the active local Recipe execution before completing upload"
        )


def _retain_version_files(store, recipe, version, old_version_id, new_version_id):
    if old_version_id == new_version_id:
        return
    storage_path = recipe["storage_path"]
    if not re.fullmatch(r"recipes/[A-Za-z0-9_-]+", storage_path) or not all(
        re.fullmatch(r"[A-Za-z0-9_-]+", value)
        for value in (old_version_id, new_version_id)
    ):
        raise InvalidState("invalid Recipe version storage path")
    directory = store.root / storage_path
    source, target = directory / old_version_id, directory / new_version_id
    for item in (directory.parent, directory, source, target):
        if item.is_symlink() or (item.exists() and not item.is_dir()):
            raise Conflict("unexpected Recipe version directory")
    source_markdown, target_markdown = source / "RECIPE.md", target / "RECIPE.md"
    for item in (source_markdown, target_markdown):
        if item.is_symlink() or (item.exists() and not item.is_file()):
            raise Conflict("unexpected Recipe version entrypoint")
    has_original = source_markdown.is_file()
    expected = (
        source_markdown.read_bytes()
        if has_original
        else render_recipe_markdown(version["content"]).encode("utf-8")
    )
    if target.is_dir():
        if not target_markdown.is_file() or target_markdown.read_bytes() != expected:
            raise Conflict("formal Recipe version directory already exists")
        return
    # Stage in the same directory so the complete tree becomes visible atomically.
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        staged = Path(temporary) / "version"
        if source.is_dir():
            shutil.copytree(source, staged, symlinks=True)
        else:
            staged.mkdir(mode=0o700)
        if not has_original:
            (staged / "RECIPE.md").write_bytes(expected)
        staged.rename(target)


def _update_executions(conn, recipe_id, version_id, new_recipe_id, new_version_id):
    def update_reference(obj, field, old, new):
        if obj.get(field) == old:
            obj[field] = new

    rows = conn.execute(
        f"SELECT id,recipe_id,recipe_version_id,base_recipe_version_id,details FROM executions WHERE {_EXECUTION_REFERENCES}",
        (
            recipe_id,
            version_id,
            version_id,
            version_id,
            recipe_id,
            version_id,
            recipe_id,
            version_id,
        ),
    ).fetchall()
    for row in rows:
        details = json.loads(row["details"])
        update_reference(
            details, "publication_base_version_id", version_id, new_version_id
        )
        update_reference(details, "recipe_id", recipe_id, new_recipe_id)
        update_reference(details, "recipe_version_id", version_id, new_version_id)
        for entry in (
            details.get("test_request"),
            *details.get("test_results", ()),
            details.get("result"),
        ):
            if entry:
                update_reference(entry, "recipe_id", recipe_id, new_recipe_id)
                update_reference(entry, "version_id", version_id, new_version_id)
                update_reference(entry, "recipe_version_id", version_id, new_version_id)
        conn.execute(
            """UPDATE executions SET recipe_id=?,recipe_version_id=?,base_recipe_version_id=?,details=? WHERE id=?""",
            (
                new_recipe_id if row["recipe_id"] == recipe_id else row["recipe_id"],
                new_version_id
                if row["recipe_version_id"] == version_id
                else row["recipe_version_id"],
                new_version_id
                if row["base_recipe_version_id"] == version_id
                else row["base_recipe_version_id"],
                json.dumps(
                    details, ensure_ascii=False, allow_nan=False, sort_keys=True
                ),
                row["id"],
            ),
        )


def promote_recipe_identity(store, conn, recipe_id, version_id, receipt):
    recipe = store.get_in(conn, recipe_id, "recipe")
    version = store.version_in(conn, recipe_id, version_id)
    recipe_id, version_id = recipe["id"], version["id"]
    new_recipe_id, new_version_id = receipt["recipe_id"], receipt["recipe_version_id"]
    for table, scope, old, new in (
        ("recipes", RECIPE_ALIAS, recipe_id, new_recipe_id),
        ("versions", VERSION_ALIAS, version_id, new_version_id),
    ):
        if old != new and (
            conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (new,)).fetchone()
            or journal_entry(conn, scope, new)
        ):
            raise Conflict(
                "cloud upload receipt is already linked to another local Recipe or version"
            )

    _retain_version_files(
        store,
        recipe,
        {**version, "id": new_version_id, "object_id": new_recipe_id},
        version_id,
        new_version_id,
    )
    conn.execute("PRAGMA defer_foreign_keys=ON")
    if recipe_id != new_recipe_id:
        conn.execute("UPDATE recipes SET id=? WHERE id=?", (new_recipe_id, recipe_id))
        conn.execute(
            "UPDATE versions SET recipe_id=?,data=json_set(data,'$.object_id',?) WHERE recipe_id=?",
            (new_recipe_id, new_recipe_id, recipe_id),
        )
        store.remember(conn, RECIPE_ALIAS, recipe_id, {}, {"id": new_recipe_id})
    if version_id != new_version_id:
        conn.execute(
            "UPDATE versions SET id=?,data=json_set(data,'$.id',?) WHERE id=?",
            (new_version_id, new_version_id, version_id),
        )
        store.remember(conn, VERSION_ALIAS, version_id, {}, {"id": new_version_id})
    if (recipe_id, version_id) != (new_recipe_id, new_version_id):
        _update_executions(conn, recipe_id, version_id, new_recipe_id, new_version_id)


def migrate_mapped_recipe_ids(store, conn):
    rows = conn.execute("""
        SELECT r.id AS local_recipe_id, v.id AS local_version_id, r.cloud_recipe_id, v.cloud_recipe_version_id
        FROM recipes r JOIN versions v ON v.recipe_id=r.id
        WHERE r.cloud_recipe_id IS NOT NULL AND v.cloud_recipe_version_id IS NOT NULL
        ORDER BY r.id, v.number
    """).fetchall()
    for row in rows:
        old_recipe, old_version = row["local_recipe_id"], row["local_version_id"]
        recipe, version = row["cloud_recipe_id"], row["cloud_recipe_version_id"]
        saved = journal_entry(conn, RECEIPT, old_version)
        if not saved or saved[1] != {"recipe_id": recipe, "recipe_version_id": version}:
            raise InvalidState("mapped Recipe version has no matching upload receipt")
        promote_recipe_identity(store, conn, old_recipe, old_version, saved[1])
        conn.execute(
            "UPDATE idempotency SET key=? WHERE scope=? AND key=?",
            (version, RECEIPT, old_version),
        )
        if not journal_entry(conn, IDENTITY, recipe):
            destination = {field: saved[0][field] for field in DESTINATION_FIELDS}
            store.remember(conn, IDENTITY, recipe, destination, {})
        conn.execute(
            "UPDATE idempotency SET key=? WHERE scope=? AND key=?",
            (recipe, PENDING, old_recipe),
        )
    conn.execute("ALTER TABLE recipes DROP COLUMN cloud_recipe_id")
    conn.execute("ALTER TABLE recipes DROP COLUMN cloud_recipe_version_id")
    conn.execute("ALTER TABLE versions DROP COLUMN cloud_recipe_version_id")
