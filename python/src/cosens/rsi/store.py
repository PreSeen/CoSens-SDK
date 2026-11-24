"""SQLite adapter: short transactions, durable queue, leases and fencing.

Executions in one data directory share the queue and concurrency coordination.
No transaction stays open while a model or tool runs.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .execution_data import compact_analysis, upgrade_execution
from .model_headers import bind_model_headers
from .models import Conflict, InputChanged, InvalidState, LeaseLost, NotFound
from .progress import stage_progress
from .recipe_file import render_recipe_markdown
from .recipe_identity import migrate_mapped_recipe_ids, recipe_id_for, version_id_for
from .store_schema import (
    LEGACY_RECIPES,
    MIGRATE_RECIPE_VERSIONS,
    RECIPE_PROJECTION,
    SCHEMA,
    STORAGE,
)
from .task import freeze_task_package, is_approved


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


EFFECTIVE_RUNTIME = """COALESCE(
    json_extract(executions.details,'$.recovery_config'),
    json_extract(executions.details,'$.runtime_config')
)"""


INITIALIZE_RECIPE_AVAILABILITY = """
WITH legacy AS MATERIALIZED (
    SELECT id, data FROM objects WHERE kind='recipe'
        AND json_type(data, '$.latest_qualified_version_id') IS NULL
),
eligible AS (
    SELECT id AS recipe_id, json_extract(data, '$.active_version_id') AS version_id
    FROM legacy
    UNION
    SELECT r.id,
        json_extract(t.value, '$.version_id')
    FROM objects e JOIN legacy r ON r.id=json_extract(e.data, '$.recipe_id')
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
        self._initialize()
        os.chmod(self.path, 0o600)

    def _initialize(self):
        with self.connect() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 9:
                return
        with self.transaction() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 9:
                return
            if version in (7, 8):
                if version == 8:
                    migrate_mapped_recipe_ids(self, conn)
                conn.execute("PRAGMA user_version = 9")
                return
            if version not in (0, 1, 2, 3, 4, 5, 6):
                raise InvalidState("unsupported RSI SQLite schema version")
            legacy = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='objects'"
            ).fetchone()
            if legacy:
                settings = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'"
                ).fetchone()
                previous_config = (
                    conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
                    if settings
                    else None
                )
                rows = conn.execute(
                    "SELECT id,data FROM objects WHERE kind='execution'"
                ).fetchall()
                for row in rows:
                    execution = upgrade_execution(json.loads(row["data"]))
                    if "runtime_config" not in execution and previous_config:
                        execution["runtime_config"] = json.loads(
                            previous_config["data"]
                        )
                    conn.execute(
                        "UPDATE objects SET data=? WHERE id=?",
                        (encode(execution), row["id"]),
                    )
                conn.execute(INITIALIZE_RECIPE_AVAILABILITY)
                conn.execute("ALTER TABLE versions RENAME TO previous_versions")
                conn.execute(LEGACY_RECIPES)
            if version == 2:
                conn.execute("ALTER TABLE executions ADD COLUMN owner TEXT")
                conn.execute("ALTER TABLE executions ADD COLUMN lease_until REAL")
                conn.execute(
                    "ALTER TABLE executions ADD COLUMN fence INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute("ALTER TABLE versions RENAME TO previous_versions")
            if version == 3:
                conn.execute("""
                    ALTER TABLE versions ADD COLUMN status TEXT
                    CHECK (status IS NULL OR (recipe_id IS NOT NULL AND status IN ('candidate', 'available', 'archived')))
                """)
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            if legacy:
                for row in conn.execute(
                    "SELECT kind,data FROM objects WHERE kind IN ('execution','material')"
                ):
                    self._write_record(
                        conn, row["kind"], json.loads(row["data"]), update=False
                    )
                conn.execute("""
                    INSERT INTO recipes (id,created_at,updated_at,archived_at,name,revision,
                        active_version_id,latest_version_id,latest_qualified_version_id,details)
                    SELECT id, json_extract(data,'$.created_at'), json_extract(data,'$.updated_at'),
                        json_extract(data,'$.archived_at'), json_extract(data,'$.name'),
                        1, json_extract(data,'$.active_version_id'),
                        json_extract(data,'$.latest_version_id'), json_extract(data,'$.latest_qualified_version_id'),
                        '{}'
                    FROM objects WHERE kind='recipe'
                """)
                conn.execute("""
                    INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key)
                    SELECT v.id, CASE WHEN o.kind='recipe' THEN o.id END,
                        CASE WHEN o.kind='rubrics' THEN o.id END,
                        CASE WHEN o.kind='rubrics' THEN json_remove(o.data, '$.latest_version_id') END,
                        v.number,v.data,v.operation_key
                    FROM previous_versions v JOIN objects o ON o.id=v.object_id
                """)
            if version == 2:
                conn.execute("""
                    INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key)
                    SELECT v.id, v.recipe_id, v.rubrics_id,
                        CASE WHEN v.rubrics_id IS NOT NULL THEN json_set(
                            json_remove(r.details, '$.latest_version_id'),
                            '$.id', r.id, '$.created_at', r.created_at, '$.updated_at', r.updated_at,
                            '$.archived_at', r.archived_at, '$.revision', r.revision
                        ) END,
                        v.number,v.data,v.operation_key
                    FROM previous_versions v LEFT JOIN rubrics r ON r.id=v.rubrics_id
                """)
            if legacy or version == 2:
                conn.execute("""
                    UPDATE executions SET owner=j.owner, lease_until=j.lease_until, fence=j.fence
                    FROM jobs j WHERE j.id=executions.id
                """)
                conn.execute("DROP TABLE jobs")
                conn.execute("DROP TABLE previous_versions")
                conn.execute("DROP TABLE IF EXISTS rubrics")
                conn.execute("DROP TABLE IF EXISTS objects")
                conn.execute("DROP TABLE IF EXISTS settings")
            if version == 4:
                conn.execute("DROP VIEW IF EXISTS recipe_records")
                conn.execute("DROP TABLE IF EXISTS recipe_api_metadata")
            upgrade = MIGRATE_RECIPE_VERSIONS if legacy or version in (2, 3) else ""
            for statement in upgrade.split(";"):
                if statement.strip():
                    conn.execute(statement)
            if legacy or version in (2, 3, 4, 5):
                conn.execute(
                    "ALTER TABLE recipes ADD COLUMN storage_path TEXT NOT NULL DEFAULT ''"
                )
                conn.execute("UPDATE recipes SET storage_path = 'recipes/' || id")
            self._migrate_recipe_content(conn)
            for statement in RECIPE_PROJECTION.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("PRAGMA user_version = 9")

    def _migrate_recipe_content(self, conn):
        rows = conn.execute("""
            SELECT r.storage_path,v.data FROM versions v JOIN recipes r ON r.id=v.recipe_id
        """).fetchall()
        for row in rows:
            version = json.loads(row["data"])
            content = version["content"]
            version["content"] = {
                "name": content["title"],
                "description": content["applicability"],
                "instructions": content["instructions"],
            }
            self._write_recipe_file(row["storage_path"], version)
            conn.execute(
                "UPDATE versions SET data=? WHERE id=?",
                (encode(version), version["id"]),
            )

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
    def _record(row):
        fields = dict(row)
        for field in ("owner", "lease_until", "fence"):
            fields.pop(field, None)
        return {**json.loads(fields.pop("details")), **fields}

    @staticmethod
    def _write_record(conn, kind, record, *, update):
        table, columns = STORAGE[kind]
        details = dict(record)
        fields = [details.pop(column, None) for column in columns]
        if kind != "recipe":
            columns = (*columns, "details")
            fields.append(encode(details))
        if update:
            assignments = ",".join(column + "=?" for column in columns[1:])
            conn.execute(
                f"UPDATE {table} SET {assignments} WHERE id=?",
                (*fields[1:], record["id"]),
            )
        else:
            names = ",".join(columns)
            placeholders = ",".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                fields,
            )

    @staticmethod
    def _rubrics_metadata(row):
        return {**json.loads(row["rubrics_metadata"]), "latest_version_id": row["id"]}

    @staticmethod
    def get_in(conn, object_id, kind=None):
        if kind is None:
            kind = object_id.partition("_")[0]
        if kind == "rubrics":
            row = conn.execute(
                "SELECT id,rubrics_metadata FROM versions WHERE rubrics_id=? ORDER BY number DESC LIMIT 1",
                (object_id,),
            ).fetchone()
            if row is None:
                raise NotFound(f"{kind} not found: {object_id}")
            return Store._rubrics_metadata(row)
        if kind not in STORAGE:
            raise NotFound(f"object not found: {object_id}")
        if kind == "recipe":
            object_id = recipe_id_for(conn, object_id)
        table = "recipe_records" if kind == "recipe" else STORAGE[kind][0]
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise NotFound(f"{kind} not found: {object_id}")
        if kind != "recipe":
            return Store._record(row)
        return dict(row)

    def get(self, object_id, kind=None):
        with self.connect() as conn:
            return self.get_in(conn, object_id, kind)

    @staticmethod
    def execution_recipe_name(conn, execution):
        recipe_id = execution.get("recipe_id") or (
            execution.get("test_request") or {}
        ).get("recipe_id")
        base_id = execution.get("base_recipe_version_id")
        if recipe_id or base_id:
            row = conn.execute(
                """SELECT name FROM recipes
                WHERE id=COALESCE(?, (SELECT object_id FROM versions WHERE id=?))""",
                (recipe_id, base_id),
            ).fetchone()
            if row and row[0]:
                return row[0]
        return execution.get("name")

    def create_recipe(self, conn, *, name=None):
        return self.insert(
            conn,
            "recipe",
            {
                "name": name,
                "latest_version_id": None,
                "active_version_id": None,
                "latest_qualified_version_id": None,
            },
        )

    @staticmethod
    def insert(conn, kind, data):
        now = time.time()
        data = {
            **data,
            "id": data.get("id", uid(kind)),
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        if kind == "recipe":
            data["storage_path"] = f"recipes/{data['id']}"
        else:
            data["revision"] = 1
        if kind != "rubrics":
            Store._write_record(conn, kind, data, update=False)
        return data

    @staticmethod
    def save(conn, data):
        kind = "recipe" if "storage_path" in data else data["id"].partition("_")[0]
        if kind == "rubrics":
            raise InvalidState("rubrics metadata is written with its version")
        data = {**data, "updated_at": time.time()}
        if kind != "recipe":
            data["revision"] += 1
        Store._write_record(conn, kind, data, update=True)
        return data

    def list(self, kind, *, archived=False, limit=50, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("limit must be 1..200; offset must be nonnegative")
        with self.connect() as conn:
            if kind == "rubrics":
                rows = conn.execute(
                    """WITH current AS (
                        SELECT id,rubrics_id,rubrics_metadata,
                               row_number() OVER (PARTITION BY rubrics_id ORDER BY number DESC) AS position
                        FROM versions WHERE rubrics_id IS NOT NULL
                    )
                    SELECT id,rubrics_metadata FROM current
                    WHERE position=1 AND (json_extract(rubrics_metadata,'$.archived_at') IS NOT NULL)=?
                    ORDER BY json_extract(rubrics_metadata,'$.created_at'),rubrics_id LIMIT ? OFFSET ?""",
                    (int(archived), limit, offset),
                )
                return [self._rubrics_metadata(row) for row in rows]
            table = "recipe_records" if kind == "recipe" else STORAGE[kind][0]
            rows = conn.execute(
                f"""SELECT * FROM {table} WHERE (archived_at IS NOT NULL)=?
                ORDER BY created_at,id LIMIT ? OFFSET ?""",
                (int(archived), limit, offset),
            ).fetchall()
            return [
                dict(row) if kind == "recipe" else self._record(row) for row in rows
            ]

    def list_executions(self, *, execution_ids=None):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM executions
                WHERE (:ids IS NULL AND archived_at IS NULL)
                   OR id IN (SELECT value FROM json_each(:ids))
                ORDER BY created_at, id""",
                {"ids": encode(execution_ids) if execution_ids is not None else None},
            )
            return [self._record(row) for row in rows]

    @staticmethod
    def version_in(conn, object_id, version_id=None):
        object_id = recipe_id_for(conn, object_id)
        version_id = version_id_for(conn, version_id)
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
            version_id = version_id_for(conn, version_id)
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
            )
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
            history = {
                (question["id"], question["revision"]): question
                for question in [*execution.get("question_history", ()), *execution["questions"]]
            }
            for question in analysis["questions"]:
                history.pop((question["id"], question["revision"]), None)
            execution.update(
                questions=analysis["questions"],
                question_history=list(history.values()),
                rubrics_version_id=rubrics_version_id,
                analysis=compact_analysis(analysis),
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
                else self.create_recipe(
                    conn, name=execution.get("name") or content["name"]
                )
            )
            if recipe["archived_at"]:
                raise InvalidState("recipe was archived during learning")
            name = recipe.get("name") or execution.get("name") or content["name"]
            version = self.append_version(
                conn,
                {**recipe, "name": name},
                {**content, "name": name},
                operation_key=operation_key,
                provenance={
                    "execution_id": execution_id,
                    "input_revision": revision,
                },
            )
            execution.update(
                name=name,
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

    def complete(self, execution_id, owner, fence, state, *, on_published=None, freeze_task_artifact=False):
        with self.transaction() as conn:
            execution = self.guard(
                conn, execution_id, owner, fence, state["input_revision"]
            )
            recipe_id = recipe_id_for(conn, state["recipe_id"])
            version_id = version_id_for(conn, state["version_id"])
            provisional = any(
                f"{q['id']}:{q['revision']}" not in execution["answers"]
                for q in execution["questions"]
            )
            result = {
                "test_status": state["test"]["status"],
                "provisional": provisional,
                "published": False,
            }
            if state["test"]["status"] == "passed" and not provisional:
                result["published"] = self.publish_recipe_version(conn, recipe_id, version_id)
                if result["published"] and on_published is not None:
                    on_published(conn)
            execution.update(
                status="completed",
                stage="done",
                progress_percent=100,
                recipe_id=recipe_id,
                recipe_version_id=version_id,
                rubrics_version_id=state["rubrics_version_id"],
                result=result,
            )
            if freeze_task_artifact and is_approved(execution):
                freeze_task_package(self, conn, execution)
            execution.pop("recovery_config", None)
            return self.save(conn, execution)

    def publish_recipe_version(self, conn, recipe_id, version_id):
        recipe = self.get_in(conn, recipe_id, "recipe")
        if recipe["archived_at"]:
            return False
        version = self.version_in(conn, recipe_id, version_id)
        recipe_id, version_id = recipe["id"], version["id"]
        latest_id = recipe.get("latest_qualified_version_id")
        if latest_id:
            latest = self.version_in(conn, recipe_id, latest_id)
            if version["number"] <= latest["number"]:
                return recipe["active_version_id"] == version_id
        conn.execute(
            "UPDATE versions SET status='archived' WHERE recipe_id=? AND number<? AND status<>'archived'",
            (recipe_id, version["number"]),
        )
        self.set_recipe_version_status(conn, recipe, version_id, "available")
        return True

    @staticmethod
    def set_recipe_version_status(conn, recipe, version_id, status):
        result = conn.execute(
            "UPDATE versions SET status=? WHERE recipe_id=? AND id=? AND status<>?",
            (status, recipe["id"], version_id, status),
        )
        if result.rowcount:
            Store.save(conn, recipe)

    def append_version(
        self,
        conn,
        obj,
        content,
        *,
        operation_key=None,
        provenance=None,
        recipe_markdown=None,
    ):
        if operation_key:
            row = conn.execute(
                "SELECT data FROM versions WHERE operation_key=?",
                (operation_key,),
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
        kind = "recipe" if "storage_path" in obj else obj["id"].partition("_")[0]
        metadata = None
        if kind == "rubrics":
            fields = {**obj, "revision": obj["revision"] + 1, "updated_at": time.time()}
            fields.pop("latest_version_id", None)
            metadata = encode(fields)
        conn.execute(
            "INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key,status) VALUES (?,?,?,?,?,?,?,?)",
            (
                version["id"],
                obj["id"] if kind == "recipe" else None,
                obj["id"] if kind == "rubrics" else None,
                metadata,
                number,
                encode(version),
                operation_key,
                "candidate" if kind == "recipe" else None,
            ),
        )
        obj["latest_version_id"] = version["id"]
        if kind == "recipe":
            self._write_recipe_file(obj["storage_path"], version, recipe_markdown)
            self.save(conn, obj)
        return version

    def _write_recipe_file(self, storage_path, version, markdown=None):
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", version["object_id"])
            or not re.fullmatch(r"[A-Za-z0-9_-]+", version["id"])
            or not re.fullmatch(r"recipes/[A-Za-z0-9_-]+", storage_path)
        ):
            raise InvalidState("invalid Recipe version storage path")
        if markdown is None:
            markdown = render_recipe_markdown(version["content"])
        directory = self.root / storage_path / version["id"]
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / "RECIPE.md")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

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

    def reserve(self, kind, owner, limit, ttl, *, retry_target=None):
        now = time.time()
        with self.transaction() as conn:
            conn.execute("DELETE FROM slots WHERE expires<?", (now,))
            if retry_target is not None:
                execution_id, fence, credential = retry_target
                if not self.has_pending(
                    [credential], execution_id=execution_id, retry_fence=fence
                ):
                    return None
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
                "UPDATE executions SET lease_until=? WHERE owner=? AND lease_until>=? AND status='running'",
                (now + ttl, owner, now),
            )

    def claim(
        self,
        owner,
        ttl,
        max_executions,
        credentials=None,
        *,
        recipe_id=None,
        execution_id=None,
        retry_fence=None,
    ):
        now = time.time()
        with self.transaction() as conn:
            running = conn.execute(
                "SELECT COUNT(*) FROM executions WHERE status='running' AND lease_until>=?",
                (now,),
            ).fetchone()[0]
            if running >= max_executions:
                return None
            row = conn.execute(
                f"""SELECT executions.id,executions.fence FROM executions
                WHERE (executions.status='queued' OR (executions.status='running' AND executions.lease_until<?))
                AND COALESCE(json_extract({EFFECTIVE_RUNTIME},'$.max_executions'),?)>?
                AND (? IS NULL OR executions.recipe_id=?)
                AND (? IS NULL OR executions.id=?)
                AND (? IS NULL OR (executions.status='queued' AND executions.fence=?
                    AND executions.archived_at IS NULL AND EXISTS (
                        SELECT 1 FROM recipes WHERE id=executions.recipe_id AND archived_at IS NULL
                    )))
                AND (? IS NULL OR json_extract({EFFECTIVE_RUNTIME},'$.model.api_key_env')
                     IN (SELECT value FROM json_each(?)))
                ORDER BY executions.created_at,executions.id LIMIT 1""",
                (
                    now,
                    max_executions,
                    running,
                    recipe_id,
                    recipe_id,
                    execution_id,
                    execution_id,
                    retry_fence,
                    retry_fence,
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
                "UPDATE executions SET owner=?,lease_until=?,fence=fence+1 WHERE id=?",
                (owner, now + ttl, row["id"]),
            )
            obj = self.get_in(conn, row["id"], "execution")
            obj["status"] = "running"
            recovery = obj.pop("recovery_config", None)
            if recovery is not None:
                obj.update(runtime_config=recovery, error=None)
            self.save(conn, obj)
            return row["id"], row["fence"] + 1

    @staticmethod
    def guard(conn, execution_id, owner, fence, input_revision=None):
        job = conn.execute(
            "SELECT owner,lease_until,fence,status FROM executions WHERE id=?",
            (execution_id,),
        ).fetchone()
        if (
            not job
            or job["owner"] != owner
            or job["fence"] != fence
            or job["lease_until"] is None
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
                obj["progress_percent"],
                stage_progress(
                    changes.get("stage"), round_number, obj["policy"]["max_rounds"]
                ),
            )
            obj.update(changes)
            return self.save(conn, obj)

    def fail_execution(self, execution_id, owner, fence, error):
        with self.transaction() as conn:
            obj = self.guard(conn, execution_id, owner, fence)
            recovery = obj.pop("recovery_config", None)
            if (
                error.get("upstream_status") in (401, 403)
                and recovery is not None
                and recovery["model"]["api_key_env"]
                != obj["runtime_config"]["model"]["api_key_env"]
            ):
                obj.update(
                    runtime_config=recovery, status="queued", stage="queued", error=None
                )
            else:
                obj.update(status="failed", stage="failed", error=error)
            return self.save(conn, obj)

    def prepare_recovery(self, config, *, recipe_id=None, previous_headers=None):
        """Refresh the runtime for jobs identified by this Client's model credentials."""
        latest_model = config["model"]
        latest_binding = latest_model["api_key_env"]
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM executions
                   WHERE (? IS NULL OR recipe_id=?)
                   AND (status IN ('queued','running') OR
                        (status='failed' AND json_extract(details,'$.error.upstream_status') IN (401,403)))""",
                (recipe_id, recipe_id),
            ).fetchall()
            for row in rows:
                obj = self._record(row)
                runtime = obj.get("runtime_config") or {}
                effective = obj.get("recovery_config") or runtime
                model = effective.get("model") or {}
                binding = model.get("api_key_env")
                scope = model.get("credential_scope")
                if scope is not None:
                    matched = scope == latest_model["credential_scope"]
                else:
                    matched = binding == latest_binding
                    if (
                        not matched
                        and previous_headers is not None
                        and model.get("base_url")
                        and model.get("model")
                    ):
                        expected, _ = bind_model_headers(
                            model["base_url"], model["model"], previous_headers
                        )
                        matched = binding == expected
                if not matched or effective == config:
                    continue
                if obj["status"] == "failed" and binding == latest_binding:
                    continue
                if obj["status"] == "running":
                    if runtime == config:
                        obj.pop("recovery_config", None)
                    else:
                        obj["recovery_config"] = config
                else:
                    obj.pop("recovery_config", None)
                    obj.update(
                        runtime_config=config,
                        status="queued",
                        stage="queued",
                        error=None,
                    )
                self.save(conn, obj)

    def pending_execution(
        self, credentials=None, *, recipe_id=None, execution_id=None, retry_fence=None
    ):
        keys = encode(credentials) if credentials is not None else None
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT executions.id, {EFFECTIVE_RUNTIME} AS config
                   FROM executions
                   WHERE executions.status IN ('queued','running')
                   AND (? IS NULL OR executions.recipe_id=?)
                   AND (? IS NULL OR executions.id=?)
                   AND (? IS NULL OR (executions.status='queued' AND executions.fence=?
                       AND executions.archived_at IS NULL AND EXISTS (
                          SELECT 1 FROM recipes WHERE id=executions.recipe_id AND archived_at IS NULL
                       )))
                   AND (? IS NULL OR json_extract({EFFECTIVE_RUNTIME},'$.model.api_key_env')
                        IN (SELECT value FROM json_each(?)))
                   ORDER BY executions.created_at,executions.id LIMIT 1""",
                (
                    recipe_id,
                    recipe_id,
                    execution_id,
                    execution_id,
                    retry_fence,
                    retry_fence,
                    keys,
                    keys,
                ),
            ).fetchone()
            return dict(row) if row else None

    def has_pending(
        self, credentials=None, *, recipe_id=None, execution_id=None, retry_fence=None
    ):
        return (
            self.pending_execution(
                credentials,
                recipe_id=recipe_id,
                execution_id=execution_id,
                retry_fence=retry_fence,
            )
            is not None
        )

    def pending_count(self, credentials, *, recipe_id=None):
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT COUNT(*) FROM executions WHERE status IN ('queued','running')
                   AND (? IS NULL OR recipe_id=?)
                   AND json_extract({EFFECTIVE_RUNTIME}, '$.model.api_key_env')
                       IN (SELECT value FROM json_each(?))""",
                (recipe_id, recipe_id, encode(credentials)),
            ).fetchone()[0]
