"""SQLite adapter: short transactions, durable queue, leases and fencing.

Executions in one data directory share the queue and concurrency coordination.
No transaction stays open while a model or tool runs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .models import Conflict, InputChanged, InvalidState, LeaseLost, NotFound
from .progress import execution_progress, stage_progress


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


INITIALIZE_RECIPE_AVAILABILITY = """
WITH legacy AS MATERIALIZED (
    SELECT id, data FROM objects WHERE kind='recipe'
        AND json_type(data, '$.latest_qualified_version_id') IS NULL
),
eligible AS (
    SELECT id AS recipe_id, json_extract(data, '$.active_version_id') AS version_id
    FROM legacy
    UNION
    SELECT json_extract(e.data, '$.result.recipe_id'),
        json_extract(t.value, '$.version_id')
    FROM objects e JOIN legacy r ON r.id=json_extract(e.data, '$.result.recipe_id')
    JOIN json_each(e.data, '$.test_results') t
    WHERE e.kind='execution' AND json_extract(e.data, '$.status')='completed'
        AND json_extract(e.data, '$.result.provisional')=0
        AND json_extract(t.value, '$.status')='passed'
        AND json_extract(t.value, '$.input_revision')=json_extract(e.data, '$.input_revision')
),
ranked AS (
    SELECT v.id, v.object_id, row_number() OVER (
        PARTITION BY v.object_id ORDER BY v.number DESC
    ) AS position
    FROM eligible e JOIN versions v ON v.object_id=e.recipe_id AND v.id=e.version_id
),
target AS MATERIALIZED (
    SELECT r.id, v.id AS version_id
    FROM legacy r LEFT JOIN ranked v ON v.object_id=r.id AND v.position=1
)
UPDATE objects
SET data=json_set(objects.data, '$.latest_qualified_version_id', target.version_id,
    '$.active_version_id', CASE WHEN json_extract(objects.data, '$.archived_at') IS NULL
        THEN target.version_id END)
FROM target WHERE objects.id=target.id
"""


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "rsi.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS objects (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind,created,id);
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES objects(id),
                    number INTEGER NOT NULL, data TEXT NOT NULL, operation_key TEXT UNIQUE,
                    UNIQUE(object_id,number));
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY REFERENCES objects(id), status TEXT NOT NULL,
                    owner TEXT, lease_until REAL, fence INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created,id);
                CREATE TABLE IF NOT EXISTS slots (
                    token TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS slots_kind ON slots(kind,expires);
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL, key TEXT NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
            """)
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
            ).fetchone():
                legacy = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
                if legacy:
                    conn.execute(
                        """UPDATE objects
                           SET data=json_set(data, '$.runtime_config', json(?))
                           WHERE kind='execution' AND json_type(data, '$.runtime_config') IS NULL""",
                        (legacy["data"],),
                    )
            if conn.execute(
                """SELECT 1 FROM objects WHERE kind='recipe'
                   AND json_type(data, '$.latest_qualified_version_id') IS NULL LIMIT 1"""
            ).fetchone():
                conn.execute(INITIALIZE_RECIPE_AVAILABILITY)
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def get_in(conn, object_id, kind=None):
        row = conn.execute(
            "SELECT kind,data FROM objects WHERE id=?", (object_id,)
        ).fetchone()
        if row is None or (kind and row["kind"] != kind):
            raise NotFound(f"{kind or 'object'} not found: {object_id}")
        return json.loads(row["data"])

    def get(self, object_id, kind=None):
        with self.connect() as conn:
            return self.get_in(conn, object_id, kind)

    @staticmethod
    def execution_recipe_name(conn, execution):
        recipe_id = (
            execution.get("recipe_id")
            or (execution.get("result") or {}).get("recipe_id")
            or (execution.get("test_request") or {}).get("recipe_id")
        )
        base_id = execution.get("base_recipe_version_id")
        if recipe_id or base_id:
            row = conn.execute(
                """SELECT json_extract(data, '$.name') FROM objects
                WHERE kind='recipe' AND id=COALESCE(?, (SELECT object_id FROM versions WHERE id=?))""",
                (recipe_id, base_id),
            ).fetchone()
            if row and row[0]:
                return row[0]
        return execution.get("name")

    @staticmethod
    def insert(conn, kind, data):
        now = time.time()
        data = {
            **data,
            "id": data.get("id", uid(kind)),
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        conn.execute(
            "INSERT INTO objects(id,kind,data,created) VALUES (?,?,?,?)",
            (data["id"], kind, encode(data), now),
        )
        if kind == "execution":
            conn.execute(
                "INSERT INTO jobs(id,status,created) VALUES (?,?,?)",
                (data["id"], data["status"], now),
            )
        return data

    @staticmethod
    def save(conn, data):
        data = {**data, "revision": data["revision"] + 1, "updated_at": time.time()}
        conn.execute(
            "UPDATE objects SET data=?,revision=? WHERE id=?",
            (encode(data), data["revision"], data["id"]),
        )
        if data["id"].startswith("execution_"):
            conn.execute(
                "UPDATE jobs SET status=? WHERE id=?", (data["status"], data["id"])
            )
        return data

    def list(self, kind, *, archived=False, limit=50, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be 1..200; offset must be nonnegative")
        with self.connect() as conn:
            # Metadata filtering in SQL; bounded, stable pagination.
            rows = conn.execute(
                """SELECT data FROM objects WHERE kind=?
                AND ((json_extract(data,'$.archived_at') IS NOT NULL)=?)
                ORDER BY created,id LIMIT ? OFFSET ?""",
                (kind, int(archived), limit, offset),
            ).fetchall()
            return [json.loads(row["data"]) for row in rows]

    @staticmethod
    def version_in(conn, object_id, version_id=None):
        row = (
            conn.execute(
                "SELECT data FROM versions WHERE object_id=? AND id=?",
                (object_id, version_id),
            ).fetchone()
            if version_id
            else conn.execute(
                "SELECT data FROM versions WHERE object_id=? ORDER BY number DESC LIMIT 1",
                (object_id,),
            ).fetchone()
        )
        if row is None:
            raise NotFound("version does not belong to this object or does not exist")
        return json.loads(row["data"])

    def version(self, object_id, version_id=None):
        with self.connect() as conn:
            return self.version_in(conn, object_id, version_id)

    def version_by_id(self, version_id, kind=None):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT object_id FROM versions WHERE id=?", (version_id,)
            ).fetchone()
            if not row:
                raise NotFound("version not found")
            if kind:
                self.get_in(conn, row[0], kind)
            return self.version_in(conn, row[0], version_id)

    def version_by_operation(self, operation_key):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM versions WHERE operation_key=?", (operation_key,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_analysis(self, execution_id, owner, fence, revision, analysis):
        with self.transaction() as conn:
            execution = self.guard(conn, execution_id, owner, fence, revision)
            key = f"rubrics:{execution_id}:{revision}"
            replay = conn.execute(
                "SELECT id FROM versions WHERE operation_key=?", (key,)
            ).fetchone()
            rubrics_version_id = (execution.get("test_request") or {}).get(
                "rubrics_version_id"
            ) or execution.get("rubrics_version_id")
            if not rubrics_version_id and replay:
                rubrics_version_id = replay[0]
            if not rubrics_version_id:
                rubric = self.insert(
                    conn,
                    "rubrics",
                    {
                        "name": "Rubrics for " + execution_id,
                        "description": "",
                        "latest_version_id": None,
                    },
                )
                rubrics_version_id = self.append_version(
                    conn,
                    rubric,
                    analysis["rubrics"],
                    operation_key=key,
                    provenance={
                        "execution_id": execution_id,
                        "input_revision": revision,
                    },
                )["id"]
            name = self.execution_recipe_name(conn, execution) or analysis.get(
                "recipe_name"
            )
            if name:
                execution["name"] = name
            execution.update(
                questions=analysis["questions"],
                hypothesis=analysis["hypothesis"],
                resolved_rubrics_version_id=rubrics_version_id,
                analysis=analysis,
                analyzed_input_revision=revision,
            )
            self.save(conn, execution)
            return rubrics_version_id

    def publish_candidate(
        self,
        execution_id,
        owner,
        fence,
        revision,
        *,
        content,
        recipe_id,
        base_id,
        operation_key,
    ):
        with self.transaction() as conn:
            execution = self.guard(conn, execution_id, owner, fence, revision)
            recipe_id = recipe_id or execution.get("recipe_id")
            if not recipe_id and base_id:
                row = conn.execute(
                    "SELECT object_id FROM versions WHERE id=?", (base_id,)
                ).fetchone()
                if not row:
                    raise NotFound("base recipe version not found")
                recipe_id = row[0]
            recipe = (
                self.get_in(conn, recipe_id, "recipe")
                if recipe_id
                else self.insert(
                    conn,
                    "recipe",
                    {
                        "name": execution.get("name") or content["title"],
                        "description": "",
                        "latest_version_id": None,
                        "active_version_id": None,
                        "latest_qualified_version_id": None,
                    },
                )
            )
            if recipe["archived_at"]:
                raise InvalidState("recipe was archived during learning")
            version = self.append_version(
                conn,
                recipe,
                {**content, "title": recipe["name"]},
                operation_key=operation_key,
                provenance={
                    "execution_id": execution_id,
                    "input_revision": revision,
                },
            )
            execution.update(
                name=recipe["name"],
                recipe_id=recipe["id"],
                recipe_version_id=version["id"],
            )
            self.save(conn, execution)
            return version

    def save_test(self, execution_id, owner, fence, revision, result):
        with self.transaction() as conn:
            execution = self.guard(conn, execution_id, owner, fence, revision)
            if not any(t["id"] == result["id"] for t in execution["test_results"]):
                execution["test_results"].append(result)
                self.save(conn, execution)

    def complete(self, execution_id, owner, fence, state):
        with self.transaction() as conn:
            execution = self.guard(
                conn, execution_id, owner, fence, state["input_revision"]
            )
            provisional = any(
                f"{q['id']}:{q['revision']}" not in execution["answers"]
                for q in execution["questions"]
            )
            result = {
                "recipe_id": state["recipe_id"],
                "recipe_version_id": state["version_id"],
                "rubrics_version_id": state["rubrics_version_id"],
                "test_status": state["test"]["status"],
                "provisional": provisional,
                "activated": False,
            }
            if state["test"]["status"] == "passed" and not provisional:
                result["activated"] = self.publish_recipe_version(
                    conn, state["recipe_id"], state["version_id"]
                )
            execution.update(
                status="completed",
                stage="done",
                progress_percent=100,
                recipe_id=state["recipe_id"],
                recipe_version_id=state["version_id"],
                result=result,
            )
            return self.save(conn, execution)

    def publish_recipe_version(self, conn, recipe_id, version_id):
        recipe = self.get_in(conn, recipe_id, "recipe")
        if recipe["archived_at"]:
            return False
        version = self.version_in(conn, recipe_id, version_id)
        latest_id = recipe.get("latest_qualified_version_id")
        if latest_id:
            latest = self.version_in(conn, recipe_id, latest_id)
            if version["number"] <= latest["number"]:
                return recipe["active_version_id"] == version_id
        recipe.update(
            active_version_id=version_id,
            latest_qualified_version_id=version_id,
        )
        self.save(conn, recipe)
        return True

    @staticmethod
    def append_version(conn, obj, content, *, operation_key=None, provenance=None):
        if operation_key:
            row = conn.execute(
                "SELECT data FROM versions WHERE operation_key=?", (operation_key,)
            ).fetchone()
            if row:
                return json.loads(row["data"])
        number = conn.execute(
            "SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE object_id=?",
            (obj["id"],),
        ).fetchone()[0]
        version = {
            "id": uid("version"),
            "object_id": obj["id"],
            "number": number,
            "content": content,
            "provenance": provenance or {},
            "created_at": time.time(),
        }
        conn.execute(
            "INSERT INTO versions VALUES (?,?,?,?,?)",
            (version["id"], obj["id"], number, encode(version), operation_key),
        )
        obj["latest_version_id"] = version["id"]
        Store.save(conn, obj)
        return version

    @staticmethod
    def replay(conn, scope, key, request):
        if not key:
            return None
        row = conn.execute(
            "SELECT request,result FROM idempotency WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row:
            if row["request"] != encode(request):
                raise Conflict("idempotency key reused with different arguments")
            return json.loads(row["result"])
        return None

    @staticmethod
    def remember(conn, scope, key, request, result):
        if key:
            conn.execute(
                "INSERT INTO idempotency VALUES (?,?,?,?)",
                (scope, key, encode(request), encode(result)),
            )

    def reserve(self, kind, owner, limit, ttl):
        now = time.time()
        with self.transaction() as conn:
            conn.execute("DELETE FROM slots WHERE expires<?", (now,))
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM slots WHERE kind=?", (kind,)
                ).fetchone()[0]
                >= limit
            ):
                return None
            token = uid("slot")
            conn.execute(
                "INSERT INTO slots VALUES (?,?,?,?)", (token, kind, owner, now + ttl)
            )
            return token

    def release(self, token):
        with self.transaction() as conn:
            conn.execute("DELETE FROM slots WHERE token=?", (token,))

    def heartbeat(self, owner, ttl, *, worker_slot=None):
        with self.transaction() as conn:
            now = time.time()
            if worker_slot and (
                conn.execute(
                    "UPDATE slots SET expires=? WHERE token=? AND owner=? AND expires>=?",
                    (now + ttl, worker_slot, owner, now),
                ).rowcount
                != 1
            ):
                raise LeaseLost("worker slot expired")
            conn.execute(
                "UPDATE slots SET expires=? WHERE owner=? AND expires>=?",
                (now + ttl, owner, now),
            )
            conn.execute(
                "UPDATE jobs SET lease_until=? WHERE owner=? AND lease_until>=? AND status='running'",
                (now + ttl, owner, now),
            )

    def claim(self, owner, ttl, max_executions, credentials=None):
        now = time.time()
        with self.transaction() as conn:
            running = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='running' AND lease_until>=?",
                (now,),
            ).fetchone()[0]
            if running >= max_executions:
                return None
            row = conn.execute(
                """SELECT jobs.id,jobs.fence FROM jobs JOIN objects ON objects.id=jobs.id
                WHERE (jobs.status='queued' OR (jobs.status='running' AND jobs.lease_until<?))
                AND COALESCE(json_extract(objects.data,'$.runtime_config.max_executions'),?)>?
                AND (? IS NULL OR json_extract(objects.data,'$.runtime_config.model.api_key_env')
                     IN (SELECT value FROM json_each(?)))
                ORDER BY jobs.created,jobs.id LIMIT 1""",
                (
                    now,
                    max_executions,
                    running,
                    *(
                        [encode(credentials)] * 2
                        if credentials is not None
                        else (None, None)
                    ),
                ),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE jobs SET owner=?,lease_until=?,fence=fence+1,status='running' WHERE id=?",
                (owner, now + ttl, row["id"]),
            )
            obj = self.get_in(conn, row["id"], "execution")
            obj["status"] = "running"
            self.save(conn, obj)
            return row["id"], row["fence"] + 1

    @staticmethod
    def guard(conn, execution_id, owner, fence, input_revision=None):
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (execution_id,)).fetchone()
        if (
            not job
            or job["owner"] != owner
            or job["fence"] != fence
            or job["lease_until"] < time.time()
            or job["status"] != "running"
        ):
            raise LeaseLost("execution cancelled or worker lease expired")
        obj = Store.get_in(conn, execution_id, "execution")
        if input_revision is not None and obj["input_revision"] != input_revision:
            raise InputChanged("execution inputs changed; restart from analysis")
        return obj

    def progress(
        self, execution_id, owner, fence, input_revision, *, round_number=1, **changes
    ):
        with self.transaction() as conn:
            obj = self.guard(conn, execution_id, owner, fence, input_revision)
            obj["progress_percent"] = max(
                execution_progress(obj),
                stage_progress(
                    changes.get("stage"), round_number, obj["policy"]["max_rounds"]
                ),
            )
            obj.update(changes)
            return self.save(conn, obj)

    def pending_execution(self, credentials=None):
        keys = encode(credentials) if credentials is not None else None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT jobs.id, json_extract(objects.data,'$.runtime_config') AS config
                   FROM jobs JOIN objects ON objects.id=jobs.id
                   WHERE jobs.status IN ('queued','running')
                   AND (? IS NULL OR json_extract(objects.data,'$.runtime_config.model.api_key_env')
                        IN (SELECT value FROM json_each(?)))
                   ORDER BY jobs.created,jobs.id LIMIT 1""",
                (keys, keys),
            ).fetchone()
            return dict(row) if row else None

    def has_pending(self, credentials=None):
        return self.pending_execution(credentials) is not None
