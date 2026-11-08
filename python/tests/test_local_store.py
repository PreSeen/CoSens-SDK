import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cosens.local.store import LocalStore, fingerprint


def test_fingerprint_matches_the_canonical_form():
    assert fingerprint({"b": 1, "a": "é"}) == fingerprint({"a": "é", "b": 1})


def test_transaction_rolls_back_on_error(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite3")
    with pytest.raises(RuntimeError):
        with store.transaction() as db:
            db.execute("INSERT INTO artifacts VALUES('a','n','c','h')")
            raise RuntimeError("boom")
    assert store.read("SELECT * FROM artifacts") == []


def test_replay_returns_saved_response_and_rejects_changed_request(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite3")
    with store.transaction() as db:
        assert store.replay(db, "op", "k", "h1") is None
        store.remember(db, "op", "k", "h1", {"id": 1})
    with store.transaction() as db:
        assert store.replay(db, "op", "k", "h1") == {"id": 1}
        with pytest.raises(ValueError, match="idempotency"):
            store.replay(db, "op", "k", "h2")


def test_second_connection_sees_committed_writes(tmp_path):
    first, second = LocalStore(tmp_path / "s.sqlite3"), LocalStore(tmp_path / "s.sqlite3")
    with first.transaction() as db:
        db.execute("INSERT INTO artifacts VALUES('a','n','c','h')")
    assert second.read_one("SELECT id FROM artifacts")["id"] == "a"


def test_foreign_keys_are_enforced(tmp_path):
    store = LocalStore(tmp_path / "s.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        with store.transaction() as db:
            db.execute("INSERT INTO executions VALUES('e','missing','original','awaiting_execution','{}','s',NULL)")


def test_many_processes_can_create_the_same_new_file(tmp_path):
    path = tmp_path / "new.sqlite3"
    script = f"from cosens.local.store import LocalStore; LocalStore({str(path)!r}).close(); print('ok')"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    processes = [subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
                 for _ in range(8)]
    results = [process.communicate(timeout=60) for process in processes]
    assert [out.strip() for out, _ in results] == ["ok"] * 8, [err[-300:] for _, err in results]


def test_reading_sees_committed_data_without_the_write_lock(tmp_path):
    first, second = LocalStore(tmp_path / "s.sqlite3"), LocalStore(tmp_path / "s.sqlite3")
    with first.reading() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM artifacts").fetchone()["n"] == 0
        second._db.execute("BEGIN IMMEDIATE")
        second._db.execute("ROLLBACK")


def test_memory_path_is_supported():
    with LocalStore(":memory:").transaction() as db:
        db.execute("INSERT INTO artifacts VALUES('a','n','c','h')")
