"""Row-based ledger; SQLite for local use, PostgreSQL for hosted deployments.

No graph snapshots. Each transaction modifies only affected rows. Database access
is internal: public reads must pass through MemoryService's live access checks.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS projects (
        tenant TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tenant,id))""",
    """CREATE TABLE IF NOT EXISTS members (
        tenant TEXT NOT NULL, project TEXT NOT NULL, principal TEXT NOT NULL,
        role TEXT NOT NULL, PRIMARY KEY(tenant,project,principal))""",
    """CREATE TABLE IF NOT EXISTS sources (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, state TEXT NOT NULL, readers TEXT NOT NULL,
        owner TEXT NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS source_versions (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL,
        recorded_at TEXT NOT NULL, PRIMARY KEY(tenant,project,id,revision))""",
    """CREATE TABLE IF NOT EXISTS receipts (
        tenant TEXT NOT NULL, project TEXT NOT NULL, principal TEXT NOT NULL,
        request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(tenant,project,principal,request_key))""",
    """CREATE TABLE IF NOT EXISTS source_context (
        tenant TEXT NOT NULL, project TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL, anchors TEXT NOT NULL,
        PRIMARY KEY(tenant,project,source_id,revision))""",
    """CREATE TABLE IF NOT EXISTS chunks (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
        start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, text TEXT NOT NULL,
        PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS source_routes (
        tenant TEXT NOT NULL, project TEXT NOT NULL, source_id TEXT NOT NULL,
        revision INTEGER NOT NULL, layer TEXT NOT NULL, route TEXT NOT NULL,
        PRIMARY KEY(tenant,project,source_id,revision))""",
    """CREATE TABLE IF NOT EXISTS assertions (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
        sequence INTEGER NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS assertion_versions (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
        value TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
        refs TEXT NOT NULL, metadata TEXT NOT NULL, valid_from TEXT NOT NULL,
        valid_to TEXT, recorded_at TEXT NOT NULL, author TEXT NOT NULL,
        PRIMARY KEY(tenant,project,id,revision))""",
    """CREATE TABLE IF NOT EXISTS changesets (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        sequence INTEGER NOT NULL, state TEXT NOT NULL, manifest TEXT NOT NULL,
        recorded_at TEXT NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS wiki_blocks (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, page TEXT NOT NULL, text TEXT NOT NULL,
        managed_by TEXT NOT NULL, author TEXT NOT NULL, refs TEXT NOT NULL,
        dependencies TEXT NOT NULL, watermark INTEGER NOT NULL,
        state TEXT NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS wiki_revisions (
        tenant TEXT NOT NULL, project TEXT NOT NULL, block_id TEXT NOT NULL,
        revision INTEGER NOT NULL, text TEXT NOT NULL, refs TEXT NOT NULL,
        recorded_at TEXT NOT NULL, PRIMARY KEY(tenant,project,block_id,revision))""",
    """CREATE TABLE IF NOT EXISTS dependencies (
        tenant TEXT NOT NULL, project TEXT NOT NULL, assertion_id TEXT NOT NULL,
        source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
        PRIMARY KEY(tenant,project,assertion_id,source_id,source_revision))""",
    """CREATE TABLE IF NOT EXISTS jobs (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        kind TEXT NOT NULL, payload TEXT NOT NULL, principal TEXT NOT NULL,
        state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT, lease_until TEXT, error_code TEXT,
        created_at TEXT NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS graph_edges (
        tenant TEXT NOT NULL, project TEXT NOT NULL, assertion_id TEXT NOT NULL,
        version INTEGER NOT NULL, subject TEXT NOT NULL, predicate TEXT NOT NULL,
        value TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL,
        PRIMARY KEY(tenant,project,assertion_id))""",
    """CREATE TABLE IF NOT EXISTS working_sets (
        tenant TEXT NOT NULL, project TEXT NOT NULL, principal TEXT NOT NULL,
        session_id TEXT NOT NULL, assertion_ids TEXT NOT NULL, expires_at TEXT NOT NULL,
        PRIMARY KEY(tenant,project,principal,session_id))""",
    "CREATE INDEX IF NOT EXISTS job_poll ON jobs(state,lease_until,created_at)",
    "CREATE INDEX IF NOT EXISTS chunk_scope ON chunks(tenant,project,source_id,source_revision)",
    "CREATE INDEX IF NOT EXISTS dependency_source ON dependencies(tenant,project,source_id)",
    "CREATE INDEX IF NOT EXISTS assertion_slot ON assertion_versions(tenant,project,subject,predicate)",
    """CREATE TABLE IF NOT EXISTS experiences (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, state TEXT NOT NULL, owner TEXT NOT NULL,
        readers TEXT NOT NULL, PRIMARY KEY(tenant,project,id))""",
    """CREATE TABLE IF NOT EXISTS experience_versions (
        tenant TEXT NOT NULL, project TEXT NOT NULL, id TEXT NOT NULL,
        revision INTEGER NOT NULL, sequence INTEGER NOT NULL,
        family TEXT NOT NULL, phase TEXT NOT NULL, environment_hash TEXT NOT NULL,
        action_schema TEXT NOT NULL, reward_spec TEXT NOT NULL, partition_name TEXT NOT NULL,
        reward_status TEXT NOT NULL, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
        recorded_at TEXT NOT NULL, PRIMARY KEY(tenant,project,id,revision))""",
    """CREATE TABLE IF NOT EXISTS experience_receipts (
        tenant TEXT NOT NULL, project TEXT NOT NULL, principal TEXT NOT NULL,
        request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL,
        PRIMARY KEY(tenant,project,principal,request_key))""",
    """CREATE INDEX IF NOT EXISTS experience_scope ON experience_versions
        (tenant,project,family,phase,environment_hash,action_schema,reward_spec,partition_name,sequence)""",
)


class Store:
    def __init__(self, connection, dialect: str):
        self.connection = connection
        self.dialect = dialect
        self._lock = threading.RLock()

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> Store:
        connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return cls(connection, "sqlite")

    @classmethod
    def postgres(cls, dsn: str) -> Store:
        import psycopg
        from psycopg.rows import dict_row

        return cls(psycopg.connect(dsn, autocommit=True, row_factory=dict_row), "postgres")

    def execute(self, sql: str, params: tuple = ()):
        # All SQL is internal, parameterized and contains no literal question marks.
        return self.connection.execute(sql.replace("?", "%s") if self.dialect == "postgres" else sql, params)

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.execute(sql, params).fetchall()]

    @contextmanager
    def transaction(self):
        with self._lock:
            self.execute("BEGIN IMMEDIATE" if self.dialect == "sqlite" else "BEGIN")
            try:
                yield self
            except BaseException:
                self.execute("ROLLBACK")
                raise
            else:
                self.execute("COMMIT")

    def migrate(self) -> None:
        with self.transaction():
            for statement in SCHEMA:
                self.execute(statement)

    def lock_project(self, tenant: str, project: str) -> dict | None:
        suffix = " FOR UPDATE" if self.dialect == "postgres" else ""
        return self.one("SELECT * FROM projects WHERE tenant=? AND id=?" + suffix, (tenant, project))

    def close(self):
        self.connection.close()
