"""Persistent turn metadata for recovery and diagnostics."""

import json
import sqlite3
import threading
from pathlib import Path

from .session_cursor import default_state_path


class TurnStore:
    def __init__(self, path=None):
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
            row = self._db.execute("SELECT value FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, turn_id, value):
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO turns(turn_id,value) VALUES(?,?) ON CONFLICT(turn_id) DO UPDATE SET value=excluded.value",
                (turn_id, json.dumps(value, default=str, ensure_ascii=False)),
            )

    def close(self):
        self._db.close()
