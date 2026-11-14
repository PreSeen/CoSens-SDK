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


# PRAGMA user_version of a store whose evidence references and learning history name executions.
STORE_VERSION = 1


def _execution_refs(value, execution_of):
    """`value` with every {revision_id, event_id} evidence reference turned into {execution_id, event_id}."""
    if isinstance(value, list):
        return [_execution_refs(item, execution_of) for item in value]
    if not isinstance(value, dict):
        return value
    if "revision_id" in value and "event_id" in value:
        value = {("execution_id" if key == "revision_id" else key): (execution_of(item) if key == "revision_id" else item)
                 for key, item in value.items()}
    return {key: _execution_refs(item, execution_of) for key, item in value.items()}


def _execution_context(context, execution_of):
    """A frozen learning context naming executions: its source, each history entry, and that entry's feedback."""
    context = dict(context)
    if "source_revision_id" in context:
        context["source_execution_id"] = execution_of(context.pop("source_revision_id"))
    history = []
    for entry in context.get("history", []):
        entry = dict(entry)
        if "revision_id" in entry:
            entry["execution_id"] = execution_of(entry.pop("revision_id"))
        if entry.get("origin") == "revision":
            entry["origin"] = "correction"
        if "feedback" in entry:
            entry["feedback"] = [{**{k: v for k, v in item.items() if k != "revision_id"}, "execution_id": execution_of(item["revision_id"])}
                                 if "revision_id" in item else item for item in entry["feedback"]]
        history.append(entry)
    if "history" in context:
        context["history"] = history
    return context


def _name_executions(db):
    """Rewrite stored learning rows written with revision keys; a row whose hash does not match its text stops the upgrade."""
    def execution_of(revision_id):
        row = db.execute("SELECT execution_id FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise LookupError(f"stored Recipe data names revision {revision_id}, which is not in this store")
        return row["execution_id"]

    for row in db.execute("SELECT id,context_json,context_sha,recipe_json,recipe_sha,attribution_json FROM recipe_candidates").fetchall():
        updates = {}
        # LocalStore writes these columns as canonical_json(value) with sha = fingerprint(value).
        for column, sha_column, rewrite in (("context_json", "context_sha", _execution_context), ("recipe_json", "recipe_sha", _execution_refs)):
            if row[column] is None:
                continue
            value = json.loads(row[column])
            renamed = rewrite(value, execution_of)
            if renamed == value:
                continue
            if fingerprint(value) != row[sha_column]:
                raise ValueError(f"Recipe candidate {row['id']}: stored {column} does not match its hash; the store was not upgraded")
            updates[column], updates[sha_column] = canonical_json(renamed), fingerprint(renamed)
        if row["attribution_json"] is not None:
            attribution = json.loads(row["attribution_json"])
            renamed = _execution_refs(attribution, execution_of)
            if renamed != attribution:
                updates["attribution_json"] = canonical_json(renamed)
        if updates:
            db.execute(f"UPDATE recipe_candidates SET {','.join(name + '=?' for name in updates)} WHERE id=?",
                       (*updates.values(), row["id"]))


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
        """Bring an older database forward in place; 0.6's unused Memory columns stay because older SQLite cannot drop them."""
        with self.transaction() as db:
            if "remote_version" not in {row["name"] for row in db.execute("PRAGMA table_info(recipe_heads)")}:
                db.execute("ALTER TABLE recipe_heads ADD COLUMN remote_version INTEGER")
                # 0.6 published to Memory, so no version is in the Recipe store yet.
                db.execute("UPDATE recipe_candidates SET publish_state='pending' WHERE publish_state IS NOT NULL")
            # Once, in this transaction: earlier stores named revisions in evidence references and learning history.
            if db.execute("PRAGMA user_version").fetchone()[0] < STORE_VERSION:
                _name_executions(db)
                db.execute(f"PRAGMA user_version={STORE_VERSION}")

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
