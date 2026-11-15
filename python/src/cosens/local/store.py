"""SQLite storage for local RSI: one file, one writer at a time, every write in a transaction."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..errors import RSIError

# Tables that version 2 rebuilt; a task is a capability, an execution names its instance, a Recipe has one task.
_TASKS = "(id TEXT PRIMARY KEY, context_json TEXT NOT NULL, context_version INTEGER NOT NULL)"
_EXECUTIONS = """(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, status TEXT NOT NULL,
  request_json TEXT NOT NULL, request_sha TEXT NOT NULL, result_json TEXT, instance_id TEXT NOT NULL)"""
_TASK_RECIPES = "(task_id TEXT NOT NULL REFERENCES tasks(id), recipe_id TEXT PRIMARY KEY REFERENCES recipe_heads(recipe_id))"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, content_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks{_TASKS};
CREATE TABLE IF NOT EXISTS executions{_EXECUTIONS};
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
CREATE TABLE IF NOT EXISTS task_recipes{_TASK_RECIPES};
CREATE TABLE IF NOT EXISTS idempotency(
  operation TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
  PRIMARY KEY(operation, request_key));
"""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


# PRAGMA user_version of the store: 1 when evidence references and learning history name executions, 2 when tasks are
# capabilities and each execution carries its own input.
STORE_VERSION = 2


class LocalStoreUpgradeError(RSIError):
    """Opening the store would need an upgrade this release cannot make; the store is left as it was."""

    def __init__(self, message, *, path, candidate_id=None, column=None):
        super().__init__(message)
        self.path = path
        self.candidate_id = candidate_id
        self.column = column


class _RowNotUpgraded(Exception):
    def __init__(self, candidate_id, column, reason):
        super().__init__(reason)
        self.candidate_id, self.column, self.reason = candidate_id, column, reason


class _StoreNotUpgraded(Exception):
    """The store holds something the upgrade must not decide on its own."""


def _rename(item, old, new, convert):
    """A copy of `item` whose key `old` becomes `new`, with its value passed through `convert`."""
    item = dict(item)
    item[new] = convert(item.pop(old))
    return item


def _execution_refs(value, execution_of):
    """`value` with every {revision_id, event_id} evidence reference turned into {execution_id, event_id}."""
    if isinstance(value, list):
        return [_execution_refs(item, execution_of) for item in value]
    if not isinstance(value, dict):
        return value
    if "revision_id" in value and "event_id" in value:
        value = _rename(value, "revision_id", "execution_id", execution_of)
    return {key: _execution_refs(item, execution_of) for key, item in value.items()}


def _execution_feedback(item, execution_of):
    if "revision_id" not in item:
        return item
    return _rename(item, "revision_id", "execution_id", execution_of)


def _execution_context(context, execution_of):
    """A frozen learning context naming executions: its source, each history entry, and that entry's feedback."""
    if "source_revision_id" in context:
        context = _rename(context, "source_revision_id", "source_execution_id", execution_of)
    if "history" not in context:
        return context
    history = []
    for entry in context["history"]:
        if "revision_id" in entry:
            entry = _rename(entry, "revision_id", "execution_id", execution_of)
        if entry.get("origin") == "revision":
            entry = {**entry, "origin": "correction"}
        if "feedback" in entry:
            feedback = []
            for item in entry["feedback"]:
                feedback.append(_execution_feedback(item, execution_of))
            entry = {**entry, "feedback": feedback}
        history.append(entry)
    return {**context, "history": history}


def _rewritten(row, column, sha_column, rewrite, detail):
    """New (text, sha) of a hashed column after `rewrite(value, detail)`, None when nothing changes; refuses a changed row."""
    if row[column] is None:
        return None
    value = json.loads(row[column])
    try:
        renamed = rewrite(value, detail)
    except LookupError as error:
        raise _RowNotUpgraded(row["id"], column, str(error)) from error
    if renamed == value:
        return None
    # LocalStore writes these columns as canonical_json(value) with sha = fingerprint(value).
    if fingerprint(value) != row[sha_column]:
        raise _RowNotUpgraded(row["id"], column, f"stored {column} does not match its hash")
    return canonical_json(renamed), fingerprint(renamed)


def _upgrade_evidence_to_executions(db):
    """Rewrite stored learning rows written with revision keys; any row it cannot rewrite stops the whole upgrade."""
    def resolver(candidate_id):
        def execution_of(revision_id):
            found = db.execute("SELECT execution_id FROM revisions WHERE id=?", (revision_id,)).fetchone()
            if found is None:
                raise LookupError(f"Recipe candidate {candidate_id} names revision {revision_id}, which is not in this store")
            return found["execution_id"]
        return execution_of

    for row in db.execute("SELECT id,context_json,context_sha,recipe_json,recipe_sha,attribution_json FROM recipe_candidates").fetchall():
        execution_of = resolver(row["id"])
        updates = {}
        context = _rewritten(row, "context_json", "context_sha", _execution_context, execution_of)
        if context is not None:
            updates["context_json"], updates["context_sha"] = context
        recipe = _rewritten(row, "recipe_json", "recipe_sha", _execution_refs, execution_of)
        if recipe is not None:
            updates["recipe_json"], updates["recipe_sha"] = recipe
        if row["attribution_json"] is not None:
            attribution = json.loads(row["attribution_json"])
            try:
                renamed = _execution_refs(attribution, execution_of)
            except LookupError as error:
                raise _RowNotUpgraded(row["id"], "attribution_json", str(error)) from error
            if renamed != attribution:
                updates["attribution_json"] = canonical_json(renamed)
        if updates:
            db.execute(f"UPDATE recipe_candidates SET {','.join(name + '=?' for name in updates)} WHERE id=?",
                       (*updates.values(), row["id"]))


def _capability_context(context, context_version):
    """A learning context frozen before version 2: each history entry's "context" (the instance input) becomes
    "input"; the context gains the learned execution's task context, which had no reference material, and no task
    materials. The task context's objective is the last entry's input objective, as migration 015 sets it."""
    history = [_rename(entry, "context", "input", lambda value: value) for entry in context["history"]]
    objective = context["history"][-1]["context"]["objective"]
    return {**context, "schema": "workflow-recipe-context-v3", "history": history,
            "task_context": {"objective": objective, "artifacts": [], "context_version": context_version},
            "task_materials": []}


def _capability_request(request):
    """A request frozen before version 2: its "context" is the execution's input, and the task context it froze is the
    objective and context version, with no reference material. The request hash stays the one frozen."""
    request = dict(request)
    value, context_version = request.pop("context"), request.pop("context_version")
    return {**request, "input": value,
            "task_context": {"objective": value["objective"], "artifacts": [], "context_version": context_version}}


def _rebuild(db, table, definition, columns, rows):
    """Replace `table` by a table of `definition` holding `rows`, each (rowid, *columns); foreign keys must be off."""
    db.execute(f"CREATE TABLE {table}_rebuilt{definition}")
    db.executemany(f"INSERT INTO {table}_rebuilt(rowid,{','.join(columns)}) VALUES(?{',?' * len(columns)})", rows)
    db.execute(f"DROP TABLE {table}")
    db.execute(f"ALTER TABLE {table}_rebuilt RENAME TO {table}")


def _upgrade_to_capability_tasks(db):
    """Version 2, as migration 015 does in the service: tasks keep only their capability, requests and start receipts
    freeze "input" and "task_context", every execution names its instance (and each start receipt its kind), learning
    contexts become v3, and a Recipe belongs to one task. Anything it cannot rewrite stops the whole upgrade."""
    if "mode" not in {row["name"] for row in db.execute("PRAGMA table_info(tasks)")}:
        return  # created by this release: nothing was written in the earlier shape
    shared = db.execute(
        "SELECT recipe_id, group_concat(task_id, ' ') AS tasks FROM "
        "(SELECT recipe_id, task_id FROM task_recipes ORDER BY recipe_id, task_id) "
        "GROUP BY recipe_id HAVING count(*) > 1 ORDER BY recipe_id").fetchall()
    if shared:
        listed = ", ".join(f"{row['recipe_id']} (tasks {row['tasks']})" for row in shared)
        raise _StoreNotUpgraded(f"Recipes bound to more than one task: {listed}. From this release on a Recipe belongs "
                                "to one task")
    executions = db.execute("SELECT rowid AS position,* FROM executions ORDER BY rowid").fetchall()
    requests = {row["id"]: json.loads(row["request_json"]) for row in executions}
    for row in db.execute("SELECT c.id,c.context_json,c.context_sha,e.id AS execution_id FROM recipe_candidates c "
                          "JOIN revisions r ON r.id=c.revision_id JOIN executions e ON e.id=r.execution_id").fetchall():
        context_version = requests[row["execution_id"]]["context_version"]
        rewritten = _rewritten(row, "context_json", "context_sha", _capability_context, context_version)
        if rewritten is not None:
            db.execute("UPDATE recipe_candidates SET context_json=?,context_sha=? WHERE id=?", (*rewritten, row["id"]))
    # An original or reconstructed execution is its own instance; a revision belongs to the instance of the execution
    # whose revision it continues.
    execution_of = dict(db.execute("SELECT id,execution_id FROM revisions").fetchall())
    instances = {}

    def instance_of(execution_id):
        chain = []
        while execution_id not in instances:
            chain.append(execution_id)
            base = requests[execution_id].get("base_revision_id")
            if base is None:
                instances[execution_id] = execution_id
            elif base not in execution_of or len(chain) > len(requests):
                raise _StoreNotUpgraded(f"execution {chain[0]} continues revision {base}, which is not in this store")
            else:
                execution_id = execution_of[base]
        for item in chain:
            instances[item] = instances[execution_id]
        return instances[execution_id]

    rebuilt = [(row["position"], row["id"], row["task_id"], row["kind"], row["status"],
                canonical_json(_capability_request(requests[row["id"]])), row["request_sha"], row["result_json"],
                instance_of(row["id"]))
               for row in executions]
    kinds = {row["id"]: row["kind"] for row in executions}
    for row in db.execute("SELECT operation,request_key,response_json FROM idempotency "
                          "WHERE operation LIKE 'start_execution:%' OR operation LIKE 'start_revision:%'").fetchall():
        response = json.loads(row["response_json"])
        response = {**response, "request": _capability_request(response["request"]),
                    "instance_id": instance_of(response["execution_id"]), "kind": kinds[response["execution_id"]]}
        db.execute("UPDATE idempotency SET response_json=? WHERE operation=? AND request_key=?",
                   (canonical_json(response), row["operation"], row["request_key"]))
    # New executions record under one operation, so a key resent for another task is refused; a key that started
    # executions of two tasks keeps its first receipt, since no request now matches either one.
    db.execute("DELETE FROM idempotency WHERE operation LIKE 'start_execution:%' AND rowid NOT IN "
               "(SELECT min(rowid) FROM idempotency WHERE operation LIKE 'start_execution:%' GROUP BY request_key)")
    db.execute("UPDATE idempotency SET operation='start_execution' WHERE operation LIKE 'start_execution:%'")
    # A task's old artifacts were the instance input, which every execution froze in its request.
    tasks = [(row["position"], row["id"],
              canonical_json({"objective": json.loads(row["context_json"])["objective"], "artifacts": []}),
              row["context_version"])
             for row in db.execute("SELECT rowid AS position,* FROM tasks ORDER BY rowid")]
    bindings = db.execute("SELECT rowid,task_id,recipe_id FROM task_recipes ORDER BY rowid").fetchall()
    _rebuild(db, "tasks", _TASKS, ("id", "context_json", "context_version"), tasks)
    _rebuild(db, "executions", _EXECUTIONS, ("id", "task_id", "kind", "status", "request_json", "request_sha",
                                             "result_json", "instance_id"), rebuilt)
    _rebuild(db, "task_recipes", _TASK_RECIPES, ("task_id", "recipe_id"), bindings)
    broken = db.execute("PRAGMA foreign_key_check").fetchone()
    if broken is not None:
        raise _StoreNotUpgraded(f"table {broken[0]} row {broken[1]} refers to a missing {broken[2]} row")


def _refresh_learning_receipts(db):
    """A learning's start receipt reports the hash of its frozen context, which each upgrade above recomputes."""
    shas = dict(db.execute("SELECT id,context_sha FROM recipe_candidates").fetchall())
    for row in db.execute("SELECT request_key,response_json FROM idempotency WHERE operation='recipe_candidate'").fetchall():
        response = json.loads(row["response_json"])
        response = {**response, "context_sha256": shas[response["recipe_candidate_id"]]}
        db.execute("UPDATE idempotency SET response_json=? WHERE operation='recipe_candidate' AND request_key=?",
                   (canonical_json(response), row["request_key"]))


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
        self._path = path
        try:
            self._upgrade()
        except BaseException:
            self._db.close()
            raise

    def _upgrade(self):
        """Bring an older database forward in place; 0.6's unused Memory columns stay because older SQLite cannot drop them."""
        # Version 2 rebuilds tables that other tables reference, which SQLite allows only with foreign keys off; the
        # upgrade checks them itself before it commits.
        self._db.execute("PRAGMA foreign_keys=OFF")
        try:
            self._upgrade_in_transaction()
        finally:
            self._db.execute("PRAGMA foreign_keys=ON")

    def _upgrade_in_transaction(self):
        with self.transaction() as db:
            if "remote_version" not in {row["name"] for row in db.execute("PRAGMA table_info(recipe_heads)")}:
                db.execute("ALTER TABLE recipe_heads ADD COLUMN remote_version INTEGER")
                # 0.6 published to Memory, so no version is in the Recipe store yet.
                db.execute("UPDATE recipe_candidates SET publish_state='pending' WHERE publish_state IS NOT NULL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > STORE_VERSION:
                raise LocalStoreUpgradeError(
                    f"LocalRSI store {self._path} is at version {version}, but this release reads version {STORE_VERSION}: "
                    "this store was written by a newer cosens-sdk. Upgrade cosens-sdk to open it.", path=self._path)
            if version == STORE_VERSION:
                return
            advice = ("; nothing was changed. To go on, open the store with the previous cosens-sdk release, or move "
                      "the file aside and start a new store.")
            # Once each, in this transaction: earlier stores named revisions in evidence references and learning
            # history (before 1), and kept the instance input on the task (before 2).
            try:
                if version < 1:
                    _upgrade_evidence_to_executions(db)
                _upgrade_to_capability_tasks(db)
                _refresh_learning_receipts(db)
            except _RowNotUpgraded as stop:
                raise LocalStoreUpgradeError(
                    f"Could not upgrade LocalRSI store {self._path}: Recipe candidate {stop.candidate_id}, "
                    f"{stop.column}: {stop.reason}{advice}",
                    path=self._path, candidate_id=stop.candidate_id, column=stop.column) from stop.__cause__
            except _StoreNotUpgraded as stop:
                raise LocalStoreUpgradeError(f"Could not upgrade LocalRSI store {self._path}: {stop}{advice}",
                                             path=self._path) from stop
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
