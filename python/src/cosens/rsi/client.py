"""Synchronous, hook-friendly SDK facade over an embedded core and local adapter."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time

from .materials import snapshot
from .models import (
    TERMINAL,
    Conflict,
    ExecutionPolicy,
    InvalidState,
    LocalConfig,
    NotFound,
    RecipeContent,
    RubricsContent,
)
from .store import Store, uid


class RSIClient:
    def __init__(
        self, *, data_dir, config: LocalConfig | dict | None = None, auto_start=True
    ):
        self.store = Store(data_dir)
        settings = (
            LocalConfig.model_validate(config).model_dump()
            if config is not None
            else None
        )
        self.config = LocalConfig.model_validate(self.store.configure(settings))
        self.auto_start = auto_start

    @property
    def data_dir(self):
        return self.store.root

    def material_create(self, *, manifest, idempotency_key=None):
        request = {"manifest": manifest}
        with self.store.transaction() as conn:
            previous = self.store.replay(
                conn, "material_create", idempotency_key, request
            )
            if previous:
                return self.store.get_in(conn, previous["id"], "material")
        data = snapshot(self.data_dir, manifest, self.config)
        try:
            with self.store.transaction() as conn:
                previous = self.store.replay(
                    conn, "material_create", idempotency_key, request
                )
                if previous:
                    shutil.rmtree(self.data_dir / "materials" / data["id"])
                    return self.store.get_in(conn, previous["id"], "material")
                obj = self.store.insert(conn, "material", data)
                self.store.remember(
                    conn, "material_create", idempotency_key, request, {"id": obj["id"]}
                )
                return obj
        except BaseException:
            shutil.rmtree(self.data_dir / "materials" / data["id"], ignore_errors=True)
            raise

    def material_get(self, material_id):
        return self.store.get(material_id, "material")

    def material_list(self, **pagination):
        return self.store.list("material", **pagination)

    def _update(
        self,
        object_id,
        kind,
        *,
        name=None,
        description=None,
        archived=None,
        expected_revision=None,
    ):
        with self.store.transaction() as conn:
            obj = self.store.get_in(conn, object_id, kind)
            if expected_revision is not None and obj["revision"] != expected_revision:
                raise Conflict("object revision changed")
            if name is not None:
                obj["name"] = name
            if description is not None:
                obj["description"] = description
            if archived is not None:
                obj["archived_at"] = (
                    (obj["archived_at"] or time.time()) if archived else None
                )
            return self.store.save(conn, obj)

    def material_update(self, material_id, **metadata):
        """Update metadata or archive; evidence and manifest remain immutable."""
        return self._update(material_id, "material", **metadata)

    def _create_versioned(self, kind, *, name, content=None):
        with self.store.transaction() as conn:
            obj = self.store.insert(
                conn,
                kind,
                {
                    "name": name,
                    "description": "",
                    "latest_version_id": None,
                    **({"active_version_id": None} if kind == "recipe" else {}),
                },
            )
            if content is not None:
                self.store.append_version(
                    conn, obj, self._validate_content(kind, content)
                )
            return self.store.get_in(conn, obj["id"], kind)

    @staticmethod
    def _validate_content(kind, content):
        return (
            (RecipeContent if kind == "recipe" else RubricsContent)
            .model_validate(content)
            .model_dump()
        )

    def rubrics_create(self, *, name, content=None):
        return self._create_versioned("rubrics", name=name, content=content)

    def recipe_create(self, *, name, content=None):
        return self._create_versioned("recipe", name=name, content=content)

    def _get_versioned(
        self,
        kind,
        object_id,
        version_id=None,
        version_selector=None,
        include_content=True,
    ):
        if version_id and version_selector:
            raise ValueError("version_id and version_selector are mutually exclusive")
        if version_selector not in {None, "active", "latest"} or (
            kind == "rubrics" and version_selector == "active"
        ):
            raise ValueError("invalid version selector")
        with self.store.connect() as conn:
            obj = self.store.get_in(conn, object_id, kind)
            selected = version_id or obj.get(
                "active_version_id"
                if kind == "recipe" and version_selector != "latest"
                else "latest_version_id"
            )
            version = (
                self.store.version_in(conn, object_id, selected) if selected else None
            )
            if version is not None and not include_content:
                version = {k: v for k, v in version.items() if k != "content"}
            return {**obj, "selected_version": version}

    def rubrics_get(self, rubrics_id, *, version_id=None, include_content=True):
        return self._get_versioned(
            "rubrics", rubrics_id, version_id, include_content=include_content
        )

    def recipe_get(
        self, recipe_id, *, version_id=None, version_selector=None, include_content=True
    ):
        return self._get_versioned(
            "recipe", recipe_id, version_id, version_selector, include_content
        )

    def rubrics_list(self, **pagination):
        return self.store.list("rubrics", **pagination)

    def recipe_list(
        self, *, q=None, active_only=False, archived=False, limit=50, offset=0
    ):
        """Local name/description/instructions search; no remote embeddings are sent."""
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT o.data FROM objects o LEFT JOIN versions v
                ON v.id=json_extract(o.data,'$.active_version_id')
                WHERE o.kind='recipe' AND ((json_extract(o.data,'$.archived_at') IS NOT NULL)=?)
                AND (?=0 OR v.id IS NOT NULL)
                AND (? IS NULL OR instr(lower(COALESCE(json_extract(o.data,'$.name'),'') || ' ' ||
                    COALESCE(json_extract(o.data,'$.description'),'') || ' ' || COALESCE(v.data,'')),lower(?))>0)
                ORDER BY o.created,o.id LIMIT ? OFFSET ?""",
                (int(archived), int(active_only), q, q, limit, offset),
            ).fetchall()
            import json

            return [json.loads(row["data"]) for row in rows]

    def rubrics_update(self, rubrics_id, **metadata):
        return self._update(rubrics_id, "rubrics", **metadata)

    def recipe_update(self, recipe_id, **metadata):
        return self._update(recipe_id, "recipe", **metadata)

    def _version_create(
        self, kind, object_id, *, content, expected_revision=None, idempotency_key=None
    ):
        content = self._validate_content(kind, content)
        request = {
            "object_id": object_id,
            "content": content,
            "expected_revision": expected_revision,
        }
        scope = kind + "_version_create"
        with self.store.transaction() as conn:
            replay = self.store.replay(conn, scope, idempotency_key, request)
            if replay:
                return replay
            obj = self.store.get_in(conn, object_id, kind)
            if obj["archived_at"]:
                raise InvalidState("restore the object before creating a version")
            if expected_revision is not None and obj["revision"] != expected_revision:
                raise Conflict("object revision changed")
            version = self.store.append_version(conn, obj, content)
            self.store.remember(conn, scope, idempotency_key, request, version)
            return version

    def rubrics_version_create(self, rubrics_id, **arguments):
        return self._version_create("rubrics", rubrics_id, **arguments)

    def recipe_version_create(self, recipe_id, **arguments):
        return self._version_create("recipe", recipe_id, **arguments)

    def _version_list(self, object_id, kind, *, limit=50, offset=0):
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        with self.store.connect() as conn:
            self.store.get_in(conn, object_id, kind)
            import json

            return [
                json.loads(row["data"])
                for row in conn.execute(
                    "SELECT data FROM versions WHERE object_id=? ORDER BY number LIMIT ? OFFSET ?",
                    (object_id, limit, offset),
                )
            ]

    def rubrics_version_list(self, rubrics_id, **pagination):
        return self._version_list(rubrics_id, "rubrics", **pagination)

    def recipe_version_list(self, recipe_id, **pagination):
        return self._version_list(recipe_id, "recipe", **pagination)

    def _execution_insert(self, conn, request):
        for material_id in request["material_ids"]:
            if self.store.get_in(conn, material_id, "material")["archived_at"]:
                raise InvalidState("archived materials cannot be attached")
        for field, kind in [
            ("rubrics_version_id", "rubrics"),
            ("base_recipe_version_id", "recipe"),
        ]:
            if request.get(field):
                row = conn.execute(
                    "SELECT object_id FROM versions WHERE id=?", (request[field],)
                ).fetchone()
                if not row:
                    raise NotFound(field + " not found")
                obj = self.store.get_in(conn, row[0], kind)
                if obj["archived_at"]:
                    raise InvalidState("archived version owner cannot be used")
        if request.get("parent_execution_id"):
            parent = self.store.get_in(
                conn, request["parent_execution_id"], "execution"
            )
            if parent["status"] not in TERMINAL:
                raise InvalidState("continue an active execution with execution_update")
        return self.store.insert(
            conn,
            "execution",
            {
                **request,
                "status": "queued",
                "stage": "queued",
                "input_revision": 1,
                "questions": [],
                "answers": request.get("answers", {}),
                "recipe_id": None,
                "recipe_version_id": None,
                "test_results": [],
                "result": None,
                "error": None,
            },
        )

    def execution_create(
        self,
        *,
        material_ids,
        policy,
        rubrics_version_id=None,
        base_recipe_version_id=None,
        parent_execution_id=None,
        source_session_id=None,
        idempotency_key=None,
    ):
        policy = ExecutionPolicy.model_validate(policy).model_dump()
        material_ids = list(dict.fromkeys(material_ids))
        if not material_ids:
            raise ValueError("at least one Material is required")
        if self.auto_start:
            self._check_runtime()
        request = {
            "material_ids": material_ids,
            "policy": policy,
            "rubrics_version_id": rubrics_version_id,
            "base_recipe_version_id": base_recipe_version_id,
            "parent_execution_id": parent_execution_id,
            "source_session_id": source_session_id,
        }
        with self.store.transaction() as conn:
            previous = self.store.replay(
                conn, "execution_create", idempotency_key, request
            )
            if previous:
                obj = self.store.get_in(conn, previous["id"], "execution")
            else:
                obj = self._execution_insert(conn, request)
                self.store.remember(
                    conn,
                    "execution_create",
                    idempotency_key,
                    request,
                    {"id": obj["id"]},
                )
        if self.auto_start:
            self.recover()
        return obj

    def execution_get(self, execution_id):
        return self.store.get(execution_id, "execution")

    def execution_list(self, **pagination):
        return self.store.list("execution", **pagination)

    def execution_update(
        self, execution_id, *, material_ids=None, archived=None, expected_revision=None
    ):
        """Append explicit evidence to active runs, or archive/restore metadata."""
        with self.store.transaction() as conn:
            obj = self.store.get_in(conn, execution_id, "execution")
            if expected_revision is not None and obj["revision"] != expected_revision:
                raise Conflict("execution revision changed")
            if material_ids is not None:
                if obj["status"] in TERMINAL:
                    raise InvalidState(
                        "terminal execution inputs are immutable; create a child execution"
                    )
                for material_id in material_ids:
                    if self.store.get_in(conn, material_id, "material")["archived_at"]:
                        raise InvalidState("archived material cannot be attached")
                updated = list(dict.fromkeys(obj["material_ids"] + list(material_ids)))
                if updated != obj["material_ids"]:
                    obj["material_ids"] = updated
                    obj["input_revision"] += 1
            if archived is not None:
                obj["archived_at"] = (
                    (obj["archived_at"] or time.time()) if archived else None
                )
            result = self.store.save(conn, obj)
        if self.auto_start and result["status"] not in TERMINAL:
            self.recover()
        return result

    def execution_cancel(self, execution_id):
        with self.store.transaction() as conn:
            obj = self.store.get_in(conn, execution_id, "execution")
            if obj["status"] == "completed":
                raise InvalidState("completed execution cannot be cancelled")
            if obj["status"] in {"failed", "cancelled"}:
                return obj
            obj.update(status="cancelled", stage="cancelled")
            result = self.store.save(conn, obj)
            conn.execute(
                "UPDATE jobs SET fence=fence+1,owner=NULL,lease_until=NULL WHERE id=?",
                (execution_id,),
            )
            return result

    def execution_clarification_answer(
        self,
        execution_id,
        *,
        question_id,
        question_revision,
        selected_option_ids,
        text=None,
        idempotency_key=None,
    ):
        result = self._clarification_answer(
            execution_id,
            question_id=question_id,
            question_revision=question_revision,
            selected_option_ids=selected_option_ids,
            text=text,
            idempotency_key=idempotency_key,
        )
        target_id = result["continuation_execution_id"] or execution_id
        if self.auto_start and self.execution_get(target_id)["status"] not in TERMINAL:
            # A replay must also repair a failed dispatch after the durable answer.
            self.recover()
        return result

    def _clarification_answer(
        self,
        execution_id,
        *,
        question_id,
        question_revision,
        selected_option_ids,
        text=None,
        idempotency_key=None,
    ):
        request = {
            "execution_id": execution_id,
            "question_id": question_id,
            "question_revision": question_revision,
            "selected_option_ids": sorted(set(selected_option_ids)),
            "text": text,
        }
        with self.store.transaction() as conn:
            previous = self.store.replay(
                conn, "clarification", idempotency_key, request
            )
            if previous:
                return previous
            obj = self.store.get_in(conn, execution_id, "execution")
            question = next(
                (q for q in obj["questions"] if q["id"] == question_id), None
            )
            if question is None or question["revision"] != question_revision:
                raise Conflict("question revision is stale or question does not exist")
            if not request["selected_option_ids"] and not text:
                raise ValueError("a selected option or free text is required")
            if set(request["selected_option_ids"]) - {
                option["id"] for option in question["options"]
            }:
                raise ValueError("unknown option")
            key = f"{question_id}:{question_revision}"
            answer = {
                "selected_option_ids": request["selected_option_ids"],
                "question": question["question"],
                "question_revision": question_revision,
                "selected_options": [
                    option
                    for option in question["options"]
                    if option["id"] in request["selected_option_ids"]
                ],
                "text": text,
            }
            previous_answer = obj["answers"].get(key)
            if previous_answer:
                if previous_answer != answer:
                    raise Conflict(
                        "answer already accepted; submit correction as new feedback Material"
                    )
                return {
                    "execution_id": execution_id,
                    "continuation_execution_id": obj.get("answer_children", {}).get(
                        key
                    ),
                }
            obj["answers"][key] = answer
            child_id = None
            if obj["status"] == "completed":
                inherited = {
                    k: obj.get(k)
                    for k in (
                        "material_ids",
                        "policy",
                        "rubrics_version_id",
                        "source_session_id",
                    )
                }
                inherited.update(
                    parent_execution_id=execution_id,
                    base_recipe_version_id=obj["recipe_version_id"],
                    answers=obj["answers"],
                )
                child_id = self._execution_insert(conn, inherited)["id"]
                obj.setdefault("answer_children", {})[key] = child_id
            elif obj["status"] not in TERMINAL:
                obj["input_revision"] += 1
            self.store.save(conn, obj)
            result = {
                "execution_id": execution_id,
                "continuation_execution_id": child_id,
            }
            self.store.remember(conn, "clarification", idempotency_key, request, result)
        return result

    def recipe_activate(self, recipe_id, *, version_id, expected_revision):
        with self.store.transaction() as conn:
            recipe = self.store.get_in(conn, recipe_id, "recipe")
            if recipe["revision"] != expected_revision:
                raise Conflict("recipe revision changed")
            if version_id is not None:
                self.store.version_in(conn, recipe_id, version_id)
                if recipe["archived_at"]:
                    raise InvalidState("restore recipe before activation")
                # Only fresh, completed, non-provisional tests are eligible.
                eligible = False
                import json

                for row in conn.execute(
                    "SELECT data FROM objects WHERE kind='execution' AND json_extract(data,'$.status')='completed'"
                ):
                    run = json.loads(row["data"])
                    if run["result"] and not run["result"].get("provisional"):
                        eligible |= any(
                            t["version_id"] == version_id
                            and t["status"] == "passed"
                            and t["input_revision"] == run["input_revision"]
                            for t in run["test_results"]
                        )
                if not eligible:
                    raise InvalidState(
                        "version has no completed, non-provisional passing test"
                    )
            recipe["active_version_id"] = version_id
            return self.store.save(conn, recipe)

    def recipe_test(
        self,
        *,
        execution_id,
        recipe_id,
        version_id,
        rubrics_version_id,
        material_ids=None,
    ):
        """Queue a real test within an active Execution, not a separate Execution."""
        with self.store.transaction() as conn:
            obj = self.store.get_in(conn, execution_id, "execution")
            if obj["status"] in TERMINAL:
                raise InvalidState(
                    "test within an active execution; create a child for a terminal run"
                )
            self.store.get_in(conn, recipe_id, "recipe")
            self.store.version_in(conn, recipe_id, version_id)
            rubric_row = conn.execute(
                "SELECT object_id FROM versions WHERE id=?", (rubrics_version_id,)
            ).fetchone()
            if rubric_row is None:
                raise NotFound("rubrics version not found")
            self.store.get_in(conn, rubric_row[0], "rubrics")
            if material_ids is not None and set(material_ids) != set(
                obj["material_ids"]
            ):
                raise Conflict(
                    "test materials must match the execution snapshot; update execution first"
                )
            request = {
                "recipe_id": recipe_id,
                "version_id": version_id,
                "rubrics_version_id": rubrics_version_id,
            }
            if obj.get("test_request") != request:
                obj["test_request"] = request
                obj["input_revision"] += 1
                self.store.save(conn, obj)
            result = {"execution_id": execution_id, "status": "queued", **request}
        if self.auto_start:
            self.recover()
        return result

    def _check_runtime(self):
        if not importlib.util.find_spec("langgraph") or not importlib.util.find_spec(
            "langchain_openai"
        ):
            raise InvalidState(
                "install cosens-sdk[local-rsi] in the Hook Python environment"
            )
        if not os.environ.get(self.config.model.api_key_env):
            raise InvalidState(
                "set the configured model API key environment variable (a nonsecret placeholder is allowed for unauthenticated local gateways)"
            )

    def recover(self):
        """Start bounded, detached workers; also reclaims expired runs after a crash.

        Call at host startup, after reboot, or from a scheduled local command.
        A stopped machine cannot automatically execute a SQLite checkpoint.
        """
        self._check_runtime()
        if not self.store.has_pending():
            return {"started": 0}
        started = 0
        for _ in range(self.config.max_executions):
            owner = uid("worker")
            slot = self.store.reserve(
                "worker", owner, self.config.max_executions, self.config.lease_seconds
            )
            if not slot:
                break
            try:
                env = dict(os.environ)
                # Editable installs and host launchers must reach the same package.
                env["PYTHONPATH"] = os.pathsep.join(str(p) for p in sys.path if p)
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "cosens.rsi.worker",
                        "--data-dir",
                        str(self.data_dir),
                        "--owner",
                        owner,
                        "--slot",
                        slot,
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
                started += 1
            except BaseException:
                self.store.release(slot)
                raise
        return {"started": started}

    def run_pending(self, *, runtime=None):
        """Foreground worker for embedding/tests; same queue, graph and lease limits."""
        from .worker import drain

        return drain(self, runtime=runtime)

    def wait(self, execution_id, *, timeout=300, poll_interval=0.2):
        deadline = time.monotonic() + timeout
        while True:
            result = self.execution_get(execution_id)
            if result["status"] in TERMINAL:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "execution is still pending; its background work has not been cancelled"
                )
            time.sleep(poll_interval)
