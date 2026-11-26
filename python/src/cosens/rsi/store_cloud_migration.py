"""Keep cloud publication data when the shared Recipe tables become Task records."""

from .models import InvalidState


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_publications(conn):
    schema = conn.execute("""
        SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND (
            (type='index' AND tbl_name='cloud_recipe_publications')
            OR (type='trigger' AND name='cloud_recipe_publication_fence')
        ) ORDER BY type,name
    """).fetchall()
    conn.execute("""
        CREATE TABLE cloud_recipe_publications_v10 (
            version_id TEXT PRIMARY KEY,
            recipe_id TEXT NOT NULL,
            execution_id TEXT REFERENCES executions(id),
            idempotency_key TEXT UNIQUE,
            requested_recipe_id TEXT,
            package_sha256 TEXT NOT NULL,
            zip_data BLOB,
            payload TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('staged','uploaded','synchronized','published','rejected')),
            receipt TEXT,
            error_status INTEGER
        )
    """)
    conn.execute("""
        INSERT INTO cloud_recipe_publications_v10 (
            version_id,recipe_id,execution_id,idempotency_key,requested_recipe_id,
            package_sha256,zip_data,payload,state,receipt,error_status
        ) SELECT version_id,recipe_id,execution_id,idempotency_key,requested_recipe_id,
            package_sha256,zip_data,payload,state,receipt,error_status
        FROM cloud_recipe_publications
    """)
    conn.execute("DROP TRIGGER IF EXISTS cloud_recipe_publication_fence")
    conn.execute("DROP TABLE cloud_recipe_publications")
    conn.execute("ALTER TABLE cloud_recipe_publications_v10 RENAME TO cloud_recipe_publications")
    for row in schema:
        conn.execute(row["sql"])


def migrate_cloud_recipe_state(conn):
    """Preserve cloud extensions before their former parent tables are dropped."""
    recipe_columns = _columns(conn, "recipes")
    if "account_id" in recipe_columns:
        owner = None
        if _columns(conn, "cloud_recipe_account"):
            owner = conn.execute("SELECT account_id FROM cloud_recipe_account WHERE singleton=1").fetchone()
        if conn.execute(
            "SELECT 1 FROM recipes WHERE account_id IS NOT NULL AND account_id IS NOT ? LIMIT 1",
            (owner["account_id"] if owner else None,),
        ).fetchone():
            raise InvalidState("cloud Recipe ownership does not match its account store")

    if "description" in recipe_columns:
        conn.execute("""
            INSERT INTO idempotency(scope,key,request,result)
            SELECT 'cloud_recipe_metadata',id,'{}',json_object('description',description)
            FROM recipes
        """)

    version_columns = _columns(conn, "versions")
    if {"object_storage_path", "package_sha256"} <= version_columns:
        conn.execute("""
            INSERT INTO idempotency(scope,key,request,result)
            SELECT 'cloud_recipe_version_metadata',id,'{}',json_object(
                'object_storage_path',object_storage_path, 'package_sha256',package_sha256
            ) FROM versions WHERE recipe_id IS NOT NULL
        """)

    if _columns(conn, "cloud_recipe_publications"):
        _migrate_publications(conn)


def backfill_cloud_task_recipe_paths(conn):
    if not _columns(conn, "cloud_recipe_publications"):
        return
    # The shared Task backfill only assigns a version to an approved execution.
    conn.execute("""
        UPDATE recipes AS task
        SET recipe_zip_path=json_extract(metadata.result,'$.object_storage_path')
        FROM cloud_recipe_publications AS publication
        JOIN idempotency AS metadata
          ON metadata.scope='cloud_recipe_version_metadata' AND metadata.key=publication.version_id
        WHERE task.recipe_zip_path IS NULL AND task.recipe_id=publication.recipe_id
          AND task.recipe_version_id=publication.version_id AND publication.state='published'
          AND publication.execution_id IS NOT NULL
          AND json_extract(metadata.result,'$.object_storage_path') IS NOT NULL
    """)
