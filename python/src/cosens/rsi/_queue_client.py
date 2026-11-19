"""Synchronous, hook-friendly SDK facade over an embedded core and local adapter."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time

from .materials import authorized_test_files, snapshot
from .models import (
    TERMINAL,
    Conflict,
    ExecutionPolicy,
    InvalidState,
    LocalConfig,
    NotFound,
)
from .progress import restart_execution
from .recipe_list import read_recipe_list
from .store import Store, uid


class RSIClient:
    def __init__(
        self, *, data_dir, config: LocalConfig | dict | None = None, auto_start=True
    ):
        self.store = Store(data_dir)
        self._supplied_config = (
            LocalConfig.model_validate(config) if config is not None else None
        )
        self.auto_start = auto_start

    @property
    def config(self):
        if self._supplied_config is None:
            raise InvalidState("config is required to create work")
        return self._supplied_config

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
            if version is not None and kind == "recipe":
                qualified_id = obj.get("latest_qualified_version_id")
                qualified = (
                    self.store.version_in(conn, object_id, qualified_id)
                    if qualified_id
                    else None
                )
                version["archived"] = bool(
                    qualified
                    and version["number"] <= qualified["number"]
                    and version["id"] != obj.get("active_version_id")
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
        """Return Recipe and learning entries with their current status."""
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        with self.store.connect() as conn:
            return read_recipe_list(
                conn,
                q=q,
                active_only=active_only,
                archived=archived,
                limit=limit,
                offset=offset,
            )

    def recipe_list_all(self, *, recipe_ids=None):
        with self.store.connect() as conn:
            return read_recipe_list(
                conn,
                q=None,
                active_only=False,
                archived=False,
                limit=-1,
                offset=0,
                recipe_ids=recipe_ids,
            )

    def _execution_insert(self, conn, request, *, config=None):
        materials = [
            self.store.get_in(conn, material_id, "material")
            for material_id in request["material_ids"]
        ]
        for material in materials:
            if material["archived_at"]:
                raise InvalidState("archived materials cannot be attached")
        authorized_test_files(
            [file for material in materials for file in material["files"]],
            request["policy"].get("test_task"),
        )
        base_id = request.get("base_recipe_version_id")
        if base_id:
            row = conn.execute(
                "SELECT object_id FROM versions WHERE id=?", (base_id,)
            ).fetchone()
            if row is None:
                raise NotFound("base_recipe_version_id not found")
            recipe = self.store.get_in(conn, row["object_id"], "recipe")
            if recipe["archived_at"]:
                raise InvalidState("archived recipe cannot be used")
        if request.get("base_execution_id"):
            parent = self.store.get_in(conn, request["base_execution_id"], "execution")
            if parent["status"] not in TERMINAL:
                raise InvalidState(
                    "continue an active execution with execution_append_evidence"
                )
        return self.store.insert(
            conn,
            "execution",
            {
                **request,
                "base_execution_id": request.get("base_execution_id"),
                "rubrics_version_id": None,
                "runtime_config": (config or self.config).model_dump(),
                "status": "queued",
                "stage": "queued",
                "progress_percent": 0,
                "input_revision": request.get("input_revision", 1),
                "questions": [],
                "answers": request.get("answers", {}),
                "recipe_id": request.get("recipe_id"),
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
        base_recipe_version_id=None,
        base_execution_id=None,
        idempotency_key=None,
    ):
        policy = ExecutionPolicy.model_validate(policy).model_dump()
        task = policy["test_task"]
        if task is not None:
            # Preserve idempotency hashes for older requests that lack these fields.
            for field in (
                "input_locators",
                "shared_reference_locators",
                "replay_current_case",
            ):
                if task[field] is None:
                    del task[field]
        material_ids = list(dict.fromkeys(material_ids))
        if not material_ids:
            raise ValueError("at least one Material is required")
        if self.auto_start:
            self._check_runtime()
        request = {
            "material_ids": material_ids,
            "policy": policy,
            "base_recipe_version_id": base_recipe_version_id,
            "base_execution_id": base_execution_id,
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

    def _write_execution_evidence(self, evidence, config, write):
        data = snapshot(self.data_dir, evidence, config)
        attached = False
        try:
            with self.store.transaction() as conn:
                execution, stored = write(conn, data)
            attached = stored
            return execution
        finally:
            if not attached:
                shutil.rmtree(self.data_dir / "materials" / data["id"])

    def _bind_recipe_on_replay(self, conn, execution_id):
        execution = self.store.get_in(conn, execution_id, "execution")
        if not execution["recipe_id"]:
            recipe_id = (execution.get("test_request") or {}).get("recipe_id")
            if not recipe_id and execution.get("base_recipe_version_id"):
                base = conn.execute(
                    "SELECT recipe_id FROM versions WHERE id=?",
                    (execution["base_recipe_version_id"],),
                ).fetchone()
                if base is None:
                    raise NotFound("base recipe version not found")
                recipe_id = base["recipe_id"]
            if not recipe_id:
                recipe_id = self.store.create_recipe(conn, name=execution.get("name"))[
                    "id"
                ]
            execution = self.store.save(conn, {**execution, "recipe_id": recipe_id})
        return {
            **execution,
            "name": self.store.execution_recipe_name(conn, execution) or "首次学习",
        }

    def execution_create_with_evidence(
        self, *, evidence, confirmation_ref, idempotency_key, base
    ):
        request = {
            "evidence": evidence,
            "confirmation_ref": confirmation_ref,
            "base": base,
        }
        scope = "execution_create"
        with self.store.connect() as conn:
            previous = self.store.replay(conn, scope, idempotency_key, request)

        def create(conn, data):
            existing = self.store.replay(conn, scope, idempotency_key, request)
            if existing:
                return self._bind_recipe_on_replay(conn, existing["id"]), False
            recipe = (
                self.store.get_in(conn, base["id"], "recipe")
                if base is not None
                else None
            )
            if recipe and (
                recipe["archived_at"] is not None or not recipe["active_version_id"]
            ):
                raise InvalidState("base Recipe has no available version")
            material = self.store.insert(conn, "material", data)
            learning_paths = {
                file["path"] for file in data["files"] if file["role"] != "input"
            }
            inputs = [file for file in data["files"] if file["role"] == "input"]
            if any(file["path"] in learning_paths for file in inputs):
                raise ValueError(
                    "current case inputs cannot also have a learning-only role"
                )
            policy = ExecutionPolicy.model_validate(
                {
                    "artifact_confirmed": True,
                    "confirmation_ref": confirmation_ref,
                    "test_task": {
                        "instruction": (
                            "Repeat the current task using the Recipe and the supplied current "
                            "inputs. Produce the expected deliverables from scratch."
                        ),
                        "replay_current_case": True,
                        "input_locators": [file["locator"] for file in inputs],
                    },
                }
            ).model_dump()
            execution = self._execution_insert(
                conn,
                {
                    "material_ids": [material["id"]],
                    "recipe_id": recipe["id"]
                    if recipe
                    else self.store.create_recipe(conn)["id"],
                    "name": recipe["name"] if recipe else None,
                    "policy": policy,
                },
            )
            self.store.remember(
                conn, scope, idempotency_key, request, {"id": execution["id"]}
            )
            return execution, True

        if previous:
            with self.store.transaction() as conn:
                execution = self._bind_recipe_on_replay(conn, previous["id"])
        else:
            if self.auto_start:
                self._check_runtime()
            execution = self._write_execution_evidence(evidence, self.config, create)
        if self.auto_start:
            self.recover()
        return execution

    def execution_append_evidence(self, execution_id, evidence):
        current = self.store.get(execution_id, "execution")

        def validate(execution):
            if execution["status"] in TERMINAL:
                raise InvalidState(
                    "terminal inputs are immutable; create a child execution"
                )

        validate(current)

        def append(conn, data):
            execution = self.store.get_in(conn, execution_id, "execution")
            validate(execution)
            for material_id in execution["material_ids"]:
                existing = self.store.get_in(conn, material_id, "material")["files"]
                if len(existing) == len(data["files"]) and all(
                    (before["path"], before["role"], before["sha256"])
                    == (after["path"], after["role"], after["sha256"])
                    for before, after in zip(existing, data["files"], strict=True)
                ):
                    return execution, False
            material = self.store.insert(conn, "material", data)
            execution["material_ids"].append(material["id"])
            restart_execution(execution)
            return self.store.save(conn, execution), True

        config = LocalConfig.model_validate(current["runtime_config"])
        result = self._write_execution_evidence(evidence, config, append)
        if self.auto_start:
            self.recover()
        return result

    def execution_evidence(self, execution):
        with self.store.connect() as conn:
            return [
                file
                for material_id in execution["material_ids"]
                for file in self.store.get_in(conn, material_id, "material")["files"]
            ]

    def execution_get(self, execution_id):
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            execution = self.store.get_in(conn, execution_id, "execution")
            execution["name"] = (
                self.store.execution_recipe_name(conn, execution) or "首次学习"
            )
            return execution

    def execution_list(self, **pagination):
        return self.store.list("execution", **pagination)

    def execution_list_all(self, *, execution_ids=None):
        return self.store.list_executions(execution_ids=execution_ids)

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
                    restart_execution(obj)
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
            return self._cancel_execution(conn, obj)

    def _cancel_execution(self, conn, execution):
        result = self.store.save(
            conn, {**execution, "status": "cancelled", "stage": "cancelled"}
        )
        conn.execute(
            "UPDATE executions SET fence=fence+1,owner=NULL,lease_until=NULL WHERE id=?",
            (execution["id"],),
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
        target_id = result["continuation_execution_id"]
        if (
            self.auto_start
            and target_id
            and self.execution_get(target_id)["status"] not in TERMINAL
        ):
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
                recorded = dict(previous_answer)
                continuation_id = recorded.pop("continuation_execution_id", None)
                if recorded != answer:
                    raise Conflict(
                        "answer already accepted; submit correction as new feedback Material"
                    )
                return {
                    "execution_id": execution_id,
                    "continuation_execution_id": continuation_id,
                }
            answers = {**obj["answers"], key: answer}
            child_id = None
            active = obj["status"] not in TERMINAL
            all_answered = all(
                f"{q['id']}:{q['revision']}" in answers for q in obj["questions"]
            )
            if all_answered and (active or obj["status"] == "completed"):
                parent = self._cancel_execution(conn, obj) if active else obj
                inherited = {
                    "material_ids": parent["material_ids"],
                    "policy": parent["policy"],
                    "base_execution_id": execution_id,
                    "recipe_id": parent["recipe_id"],
                    "base_recipe_version_id": parent["recipe_version_id"]
                    or parent.get("base_recipe_version_id"),
                    # Keep newly generated questions distinct from inherited answers.
                    "input_revision": max(
                        parent["input_revision"],
                        *(answer["question_revision"] for answer in answers.values()),
                    )
                    + 1,
                    "answers": answers,
                }
                name = self.store.execution_recipe_name(conn, parent)
                if name:
                    inherited["name"] = name
                if active and parent.get("test_request"):
                    inherited["test_request"] = parent["test_request"]
                child_id = self._execution_insert(
                    conn,
                    inherited,
                    config=self._supplied_config
                    or LocalConfig.model_validate(parent["runtime_config"]),
                )["id"]
                answers = {
                    **answers,
                    key: {**answer, "continuation_execution_id": child_id},
                }
                obj = parent
            self.store.save(conn, {**obj, "answers": answers})
            result = {
                "execution_id": execution_id,
                "continuation_execution_id": child_id,
            }
            self.store.remember(conn, "clarification", idempotency_key, request, result)
        return result

    def recipe_version_set_archived(self, recipe_id, *, version_id, archived):
        with self.store.transaction() as conn:
            recipe = self.store.get_in(conn, recipe_id, "recipe")
            version = self.store.version_in(conn, recipe_id, version_id)
            latest_id = recipe.get("latest_qualified_version_id")
            latest = (
                self.store.version_in(conn, recipe_id, latest_id) if latest_id else None
            )
            if not latest or version["number"] > latest["number"]:
                raise InvalidState(
                    "version is not eligible for archival or restoration"
                )
            if version_id != latest_id:
                if not archived:
                    raise InvalidState(
                        "only the latest qualified version can be restored"
                    )
            else:
                if not archived and recipe["archived_at"]:
                    raise InvalidState("recipe is archived")
                self.store.set_recipe_version_status(
                    conn, recipe, version_id, "archived" if archived else "available"
                )
            return {
                "recipe_id": recipe_id,
                "version_id": version_id,
                "archived": archived,
            }

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
                if obj["recipe_id"] and obj["recipe_id"] != recipe_id:
                    timestamp = time.time()
                    conn.execute(
                        """UPDATE recipes SET archived_at=?, updated_at=?
                           WHERE id=? AND archived_at IS NULL
                             AND NOT EXISTS (
                               SELECT 1 FROM versions WHERE recipe_id=recipes.id
                             )
                             AND NOT EXISTS (
                               SELECT 1 FROM executions WHERE recipe_id=recipes.id AND id<>?
                             )""",
                        (timestamp, timestamp, obj["recipe_id"], execution_id),
                    )
                    obj["recipe_id"] = recipe_id
                obj["test_request"] = request
                restart_execution(obj)
                self.store.save(conn, obj)
            result = {"execution_id": execution_id, "status": "queued", **request}
        if self.auto_start:
            self.recover()
        return result

    def _check_runtime(self, config=None):
        if not importlib.util.find_spec("langgraph"):
            raise InvalidState(
                "install cosens-sdk[local-rsi] in the Hook Python environment"
            )
        if not os.environ.get((config or self.config).model.api_key_env):
            raise InvalidState(
                "set the configured model API key environment variable (a nonsecret placeholder is allowed for unauthenticated local gateways)"
            )

    def recover(self):
        """Dispatch detached workers, or schedule one to wait for an expired lease."""
        first = self.store.pending_execution()
        if not first:
            return {"started": 0}
        credentials = [name for name, value in os.environ.items() if value]
        pending = self.store.pending_execution(credentials) or first
        config = LocalConfig.model_validate_json(pending["config"])
        self._check_runtime(config)

        def launch(owner, slot, *, deferred=False):
            env = dict(os.environ)
            # Editable installs and host launchers must reach the same package.
            env["PYTHONPATH"] = os.pathsep.join(str(p) for p in sys.path if p)
            command = [
                sys.executable,
                "-m",
                "cosens.rsi.worker",
                "--data-dir",
                str(self.data_dir),
                "--owner",
                owner,
                "--slot",
                slot,
                "--execution-id",
                pending["id"],
            ]
            if deferred:
                command.append("--wait-for-slot")
            try:
                subprocess.Popen(
                    command,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                )
            except BaseException:
                self.store.release(slot)
                raise

        started = 0
        for _ in range(config.max_executions):
            owner = uid("worker")
            slot = self.store.reserve(
                "worker", owner, config.max_executions, config.lease_seconds
            )
            if not slot:
                break
            launch(owner, slot)
            started += 1
        if started:
            return {"started": started}

        owner = uid("worker")
        deferred = self.store.reserve("recovery", owner, 1, config.lease_seconds)
        if not deferred:
            return {"started": 0}
        launch(owner, deferred, deferred=True)
        return {"started": 0, "scheduled": True}

    def run_pending(self, *, runtime=None):
        """Foreground worker for embedding/tests; same queue, graph and lease limits."""
        from .worker import drain

        if self._supplied_config:
            return drain(self, runtime=runtime)
        pending = self.store.pending_execution()
        if not pending:
            return {"processed": 0}
        client = RSIClient(
            data_dir=self.data_dir,
            config=LocalConfig.model_validate_json(pending["config"]),
            auto_start=False,
        )
        return drain(client, runtime=runtime)

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
