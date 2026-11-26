import hashlib
import json
import sqlite3

import pytest

from cosens.rsi.models import InvalidState
from cosens.rsi.recipe_package import build_recipe_package
from cosens.rsi.store import Store


def _legacy_cloud_store(tmp_path, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(Store, "_migrate_task_recipes", lambda self, conn: None)
        store = Store(tmp_path)
    states = ("staged", "uploaded", "synchronized", "published", "rejected")
    with store.connect() as conn:
        conn.executescript("""
            ALTER TABLE recipes ADD COLUMN account_id TEXT;
            ALTER TABLE recipes ADD COLUMN description TEXT;
            ALTER TABLE versions ADD COLUMN object_storage_path TEXT;
            ALTER TABLE versions ADD COLUMN package_sha256 TEXT;
            CREATE TABLE cloud_recipe_account (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1), account_id TEXT NOT NULL
            );
            INSERT INTO cloud_recipe_account VALUES (1,'owner');
            CREATE TABLE cloud_recipe_publications (
                version_id TEXT PRIMARY KEY REFERENCES versions(id),
                recipe_id TEXT NOT NULL REFERENCES recipes(id),
                execution_id TEXT REFERENCES executions(id),
                idempotency_key TEXT UNIQUE,
                requested_recipe_id TEXT,
                package_sha256 TEXT NOT NULL,
                zip_data BLOB,
                payload TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('staged','uploaded','synchronized','published','rejected')),
                receipt TEXT,
                error_status INTEGER
            );
            CREATE TRIGGER cloud_recipe_publication_fence
                BEFORE UPDATE OF input_revision, status ON executions
                WHEN (NEW.input_revision <> OLD.input_revision OR NEW.status='cancelled')
                  AND EXISTS (SELECT 1 FROM cloud_recipe_publications WHERE execution_id=OLD.id
                              AND state IN ('staged','uploaded','synchronized'))
                BEGIN SELECT RAISE(ABORT, 'cloud_recipe_publication_in_progress'); END;
            CREATE UNIQUE INDEX cloud_recipe_one_publication
                ON cloud_recipe_publications(recipe_id) WHERE state IN ('staged','uploaded','synchronized');
        """)
    with store.transaction() as conn:
        store.insert(conn, "execution", {
            "id": "execution_pending", "recipe_id": "recipe_staged", "status": "running",
            "stage": "analysis", "progress_percent": 0, "input_revision": 1, "answers": {},
        })
        for state in states:
            recipe = store.insert(conn, "recipe", {"id": f"recipe_{state}", "name": state})
            recipe_id = recipe["id"]
            conn.execute("UPDATE recipes SET account_id='owner', description=? WHERE id=?", (state, recipe_id))
            version_id = f"version_{state}"
            store.append_version(conn, recipe, {
                "name": state, "description": state, "instructions": "Fixture instructions.",
            }, version_id=version_id)
            conn.execute("UPDATE versions SET object_storage_path=?,package_sha256=? WHERE id=?",
                         (f"objects/{version_id}", "a" * 64, version_id))
            conn.execute("INSERT INTO cloud_recipe_publications VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                version_id, recipe_id, "execution_pending" if state == "staged" else None,
                f"key_{state}", recipe_id, "a" * 64, b"zip" if state == "staged" else None,
                '{"payload":true}', state, '{"receipt":true}' if state in {"synchronized", "published"} else None,
                409 if state == "rejected" else None,
            ))
    return store


def test_cloud_v9_migration_preserves_publications_projections_and_constraints(tmp_path, monkeypatch):
    legacy = _legacy_cloud_store(tmp_path, monkeypatch)
    with legacy.connect() as conn:
        rows = conn.execute("SELECT * FROM cloud_recipe_publications ORDER BY version_id").fetchall()
    migrated = Store(tmp_path)
    with migrated.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert conn.execute("SELECT * FROM cloud_recipe_publications ORDER BY version_id").fetchall() == rows
        assert conn.execute("SELECT account_id FROM cloud_recipe_account").fetchone()[0] == "owner"
        assert [row["name"] for row in conn.execute("PRAGMA table_info(recipes)")] == [
            "id", "recipe_id", "recipe_version_id", "recipe_zip_path",
        ]
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='versions'").fetchone()[0] == "view"
        assert [row["table"] for row in conn.execute("PRAGMA foreign_key_list(cloud_recipe_publications)")] == ["executions"]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        results = {
            (row["scope"], row["key"]): json.loads(row["result"])
            for row in conn.execute("SELECT scope,key,result FROM idempotency WHERE scope LIKE 'cloud_recipe%'")
        }
        for row in rows:
            assert results[("cloud_recipe_metadata", row["recipe_id"])] == {"description": row["state"]}
            assert results[("cloud_recipe_version_metadata", row["version_id"])] == {
                "object_storage_path": f"objects/{row['version_id']}", "package_sha256": row["package_sha256"],
            }
        with pytest.raises(sqlite3.IntegrityError, match="cloud_recipe_publication_in_progress"):
            conn.execute("UPDATE executions SET input_revision=2 WHERE id='execution_pending'")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute("""
                INSERT INTO cloud_recipe_publications(version_id,recipe_id,idempotency_key,package_sha256,payload,state)
                VALUES ('version_second','recipe_staged','new_key','digest','{}','staged')
            """)


def test_cloud_v9_migration_rejects_conflicting_account_without_rewriting_tables(tmp_path, monkeypatch):
    legacy = _legacy_cloud_store(tmp_path, monkeypatch)
    with legacy.transaction() as conn:
        conn.execute("UPDATE recipes SET account_id='other' WHERE id='recipe_published'")
    with pytest.raises(InvalidState, match="ownership does not match"):
        Store(tmp_path)
    with legacy.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT description,account_id FROM recipes WHERE id='recipe_published'").fetchone()[:] == (
            "published", "other",
        )
        assert conn.execute("SELECT COUNT(*) FROM cloud_recipe_publications").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM idempotency WHERE scope LIKE 'cloud_recipe%'").fetchone()[0] == 0


@pytest.mark.parametrize("object_path", ["s3://recipe-bucket/version_published.zip", None])
def test_cloud_v9_backfills_only_missing_approved_task_paths(tmp_path, monkeypatch, object_path):
    legacy = _legacy_cloud_store(tmp_path, monkeypatch)
    archive = build_recipe_package(tmp_path / "recipes" / "recipe_published" / "version_published")
    cache = tmp_path / "task-packages" / "execution_cached.zip"
    cache.parent.mkdir()
    cache.write_bytes(archive)
    approved = {"test_status": "passed", "published": True, "provisional": False}
    with legacy.transaction() as conn:
        for task, version, result in (
            ("finished", "published", approved),
            ("cached", "published", approved),
            ("unapproved", "published", {**approved, "provisional": True}),
            ("uploaded", "uploaded", approved),
        ):
            legacy.insert(conn, "execution", {
                "id": f"execution_{task}", "recipe_id": f"recipe_{version}",
                "recipe_version_id": f"version_{version}", "status": "completed",
                "stage": "complete", "progress_percent": 100, "input_revision": 1,
                "answers": {}, "result": result,
            })
        conn.execute("UPDATE cloud_recipe_publications SET execution_id='execution_finished' WHERE state='published'")
        conn.execute("UPDATE cloud_recipe_publications SET execution_id='execution_uploaded' WHERE state='uploaded'")
        conn.execute("UPDATE versions SET object_storage_path=? WHERE id='version_published'", (object_path,))
        legacy.remember(conn, "task_final_package", "execution_cached", {
            "execution_id": "execution_cached", "version_id": "version_published",
            "sha256": hashlib.sha256(archive).hexdigest(),
        }, {})

    with Store(tmp_path).connect() as conn:
        rows = dict(conn.execute("SELECT id,recipe_zip_path FROM recipes"))
        assert rows["execution_finished"] == object_path
        assert rows["execution_cached"] == "task-packages/execution_cached.zip"
        assert rows["execution_unapproved"] is None
        assert rows["execution_uploaded"] is None
    assert cache.read_bytes() == archive
