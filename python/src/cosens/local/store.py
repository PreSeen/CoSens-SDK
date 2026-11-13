"""SQLite storage for local RSI: one file, one writer at a time, every write in a transaction."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, mode TEXT NOT NULL, context_json TEXT NOT NULL, context_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS executions(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, status TEXT NOT NULL,
  request_json TEXT NOT NULL, request_sha TEXT NOT NULL, result_json TEXT);
CREATE TABLE IF NOT EXISTS revisions(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
  execution_id TEXT NOT NULL REFERENCES executions(id), result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(
  id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions(id),
  request_key TEXT NOT NULL UNIQUE, body_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recipe_candidates(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), revision_id TEXT, state TEXT NOT NULL,
  phase TEXT NOT NULL DEFAULT 'queued', context_json TEXT NOT NULL, context_sha TEXT NOT NULL, recipe_json TEXT,
  recipe_sha TEXT, attribution_json TEXT, error TEXT, generation INTEGER NOT NULL DEFAULT 1,
  lease_expires_at REAL, publish_state TEXT);
CREATE TABLE IF NOT EXISTS recipe_heads(
  recipe_id TEXT PRIMARY KEY, current_candidate_id TEXT, latest_candidate_id TEXT NOT NULL, remote_version INTEGER);
CREATE TABLE IF NOT EXISTS recipe_versions(
  candidate_id TEXT PRIMARY KEY REFERENCES recipe_candidates(id), recipe_id TEXT NOT NULL REFERENCES recipe_heads(recipe_id),
  version INTEGER NOT NULL, parent_candidate_id TEXT, UNIQUE(recipe_id, version));
CREATE TABLE IF NOT EXISTS task_recipes(
  task_id TEXT PRIMARY KEY REFERENCES tasks(id), recipe_id TEXT NOT NULL REFERENCES recipe_heads(recipe_id));
CREATE TABLE IF NOT EXISTS idempotency(
  operation TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  PRIMARY KEY(operation, request_key));
"""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def default_store_path():
    return Path.home() / ".cosens" / "local-rsi.sqlite3"


class LocalStore:
    def __init__(self, path=None):
        path = str(path or default_store_path())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._upgrade()

    def _upgrade(self):
        """Bring a 0.6 database forward in place; its unused Memory columns stay because older SQLite cannot drop them."""
        with self.transaction() as db:
            if "remote_version" not in {row["name"] for row in db.execute("PRAGMA table_info(recipe_heads)")}:
                db.execute("ALTER TABLE recipe_heads ADD COLUMN remote_version INTEGER")
                # 0.6 published to Memory, so no version is in the Recipe store yet.
                db.execute("UPDATE recipe_candidates SET publish_state='pending' WHERE publish_state IS NOT NULL")

    @contextmanager
    def transaction(self):
        """Serialize writers across threads and processes; roll back on any exception."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    @contextmanager
    def reading(self):
        """A consistent view for several reads, without taking the write lock."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                yield self._db
            finally:
                self._db.execute("COMMIT")

    def read(self, sql, parameters=()):
        with self._lock:
            return self._db.execute(sql, parameters).fetchall()

    def read_one(self, sql, parameters=()):
        rows = self.read(sql, parameters)
        return rows[0] if rows else None

    @staticmethod
    def replay(db, operation, key, request_hash):
        """Return the saved response for this key, or None; a different request under the key is an error."""
        row = db.execute(
            "SELECT request_hash,response_json FROM idempotency WHERE operation=? AND request_key=?", (operation, key)
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise ValueError("idempotency key reused with a different request")
        return json.loads(row["response_json"])

    @staticmethod
    def remember(db, operation, key, request_hash, response):
        db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (operation, key, request_hash, canonical_json(response)))

    def close(self):
        with self._lock:
            self._db.close()
