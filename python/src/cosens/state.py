"""Persistent turn metadata for recovery and diagnostics."""

import json
import hashlib
import sqlite3
import threading
from pathlib import Path

from ._config import resolve_service
from .session_cursor import default_state_path


class TurnStore:
    def __init__(self, path=None, *, principal=None, service_id=None):
        self._namespace = (service_id or resolve_service().identity) + ":" + hashlib.sha256(json.dumps(principal, sort_keys=True).encode()).hexdigest() + ":"
        path = str(path or default_state_path())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._db.commit()
        self._lock = threading.RLock()

    def get(self, turn_id):
        with self._lock:
            row = self._db.execute("SELECT value FROM turns WHERE turn_id=?", (self._namespace + turn_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, turn_id, value):
        self.put_many([(turn_id, value)])

    def put_many(self, entries):
        with self._lock, self._db:
            for turn_id, value in entries:
                self._db.execute(
                    "INSERT INTO turns(turn_id,value) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET value=excluded.value",
                    (self._namespace + turn_id, json.dumps(value, default=str, ensure_ascii=False)),
                )

    def claim_state(self, turn_id, *, expected_state, value):
        """Atomically claim a persisted turn before dispatching native side effects."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM turns WHERE turn_id=?", (self._namespace + turn_id,)).fetchone()
                current = json.loads(row[0]) if row else None
                if (not current or current.get("state") != expected_state or
                        current.get("execution_id") != value.get("execution_id") or
                        current.get("request_sha256") != value.get("request_sha256")):
                    self._db.rollback()
                    return False
                self._db.execute("UPDATE turns SET value=? WHERE turn_id=?",
                                 (json.dumps(value, default=str, ensure_ascii=False), self._namespace + turn_id))
                self._db.commit()
                return True
            except BaseException:
                self._db.rollback()
                raise

    def insert_if_absent(self, turn_id, value):
        """Create a turn once across processes sharing the same state file."""
        with self._lock, self._db:
            cursor = self._db.execute("INSERT OR IGNORE INTO turns(turn_id,value) VALUES(?,?)",
                                      (self._namespace + turn_id, json.dumps(value, default=str, ensure_ascii=False)))
            return cursor.rowcount == 1

    def update_pending(self, turn_id, value):
        """Update pre-execution metadata without rolling back a native dispatch."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT value FROM turns WHERE turn_id=?", (self._namespace + turn_id,)).fetchone()
                current = json.loads(row[0]) if row else None
                if (not current or current.get("state") != "pre_execution_pending" or
                        current.get("task_spec") != value.get("task_spec") or current.get("mode") != value.get("mode") or
                        (current.get("task_id") and current["task_id"] != value.get("task_id")) or
                        (current.get("execution_id") and current["execution_id"] != value.get("execution_id")) or
                        (current.get("request_sha256") and current["request_sha256"] != value.get("request_sha256"))):
                    self._db.rollback()
                    return False
                self._db.execute("UPDATE turns SET value=? WHERE turn_id=?",
                                 (json.dumps(value, default=str, ensure_ascii=False), self._namespace + turn_id))
                self._db.commit()
                return True
            except BaseException:
                self._db.rollback()
                raise

    def close(self):
        self._db.close()
