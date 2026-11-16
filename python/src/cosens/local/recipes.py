"""Recipe candidates and version chains: creation, generation, failure and publication."""

import asyncio
import json
import logging
import time
from uuid import uuid4

from ..atoms_helpers import recipe_request_key, stable_key
from ..errors import RSICandidateFailed, RSIError, RSIHTTPError
from ..models import result
from ..workflow_atoms import _RECIPE_ID, _job_timeout
from .models import ModelUnavailable
from .publish import check_upload, upload_form, without_evidence
from .recipe_stages import (MAX_CONTEXT_BYTES, MAX_HISTORY_REVISIONS, MODEL_OUTPUT_ATTEMPTS, ModelOutputInvalid, WebPlan,
                            run_recipe_stages)
from .store import canonical_json, fingerprint
from .web_search import WebSearchUnavailable, validate_blocked_domains

_IN_FLIGHT = ("queued", "awaiting_parent", "running")
_LIST_STATES = {"queued", "awaiting_parent", "running", "awaiting_material", "candidate", "failed", "interrupted"}
_STAGES = 3  # attribution, web research, generation
_LOG = logging.getLogger("cosens.local")
PARENT_FAILED = {"code": "parent_failed", "message": "The Recipe this candidate builds on failed; resume it first.",
                 "retryable": True, "phase": None, "upstream_status": None, "upstream_code": None}
INTERRUPTED = {"code": "interrupted", "message": "The process generating this candidate stopped; resume to retry.",
               "retryable": True, "phase": None, "upstream_status": None, "upstream_code": None}


class ParentChanged(ValueError):
    """The Recipe this candidate builds on is no longer the current version."""


def _failure_detail(exc, phase):
    detail = {"phase": phase, "upstream_status": None, "upstream_code": None}
    if isinstance(exc, ModelUnavailable):
        return {"code": exc.code, "message": str(exc), "retryable": exc.retryable, **detail,
                "upstream_status": exc.upstream_status, "upstream_code": exc.upstream_code}
    if isinstance(exc, WebSearchUnavailable):
        return {"code": "web_search_unavailable", "retryable": exc.retryable, **detail,
                "message": "Web search did not answer; resume to retry." if exc.retryable
                else "Web search was requested but none is configured."}
    if isinstance(exc, ModelOutputInvalid):
        return {"code": "model_output_invalid", "retryable": True, **detail,
                "message": f"Model output failed validation after {MODEL_OUTPUT_ATTEMPTS} attempts."}
    if isinstance(exc, ParentChanged):
        return {"code": "parent_changed", "retryable": False, **detail,
                "message": "The parent Recipe changed; generate a new candidate."}
    return {"code": "internal_error", "retryable": True, **detail, "message": "Recipe generation failed unexpectedly."}


class LocalRecipeLifecycle:
    @staticmethod
    def _head(db, task_id):
        return db.execute("SELECT h.* FROM task_recipes t JOIN recipe_heads h ON h.recipe_id=t.recipe_id WHERE t.task_id=?",
                          (task_id,)).fetchone()

    @staticmethod
    def _lineage(db, candidate_id):
        row = db.execute("SELECT v.*,h.current_candidate_id,h.latest_candidate_id FROM recipe_versions v "
                         "JOIN recipe_heads h ON h.recipe_id=v.recipe_id WHERE v.candidate_id=?", (candidate_id,)).fetchone()
        if row is None:
            raise LookupError("Recipe version not found")
        return dict(row)

    def _checked_view(self, db, row):
        """The candidate's view, refusing stored content that no longer matches its hash."""
        if fingerprint(json.loads(row["context_json"])) != row["context_sha"]:
            raise ValueError("Recipe context changed")
        if row["recipe_json"] and fingerprint(json.loads(row["recipe_json"])) != row["recipe_sha"]:
            raise ValueError("Recipe content changed")
        return self._view(db, row)

    def _view(self, db, row):
        context = json.loads(row["context_json"])
        lineage = self._lineage(db, row["id"])
        attribution = json.loads(row["attribution_json"]) if row["attribution_json"] else None
        models = attribution.pop("models", None) if attribution else None
        web = attribution.pop("web", None) if attribution else None
        detail = json.loads(row["error"]) if row["error"] else None
        return {"recipe_candidate_id": row["id"], "recipe_id": lineage["recipe_id"], "version": lineage["version"],
                "parent_candidate_id": lineage["parent_candidate_id"], "current": lineage["current_candidate_id"] == row["id"],
                "task_id": row["task_id"], "revision_id": row["revision_id"], "status": row["state"],
                "context_sha256": row["context_sha"], "recipe_sha256": row["recipe_sha"], "phase": row["phase"],
                "attribution": attribution, "recipe": json.loads(row["recipe_json"]) if row["recipe_json"] else None,
                "error": detail["code"] if detail else None, "error_detail": detail, "models": models,
                "web_search": web, "memory_context": context["memory"]["status"], "initial": bool(context.get("initial", False)),
                "material_gap": context.get("material_gap"), "publish_state": row["publish_state"]}

    def _candidate(self, db, candidate_id):
        row = db.execute("SELECT * FROM recipe_candidates WHERE id=?", (candidate_id,)).fetchone()
        if row is None:
            raise LookupError("recipe candidate not found")
        return row

    def _method(self, db, recipe_id):
        head = db.execute("SELECT * FROM recipe_heads WHERE recipe_id=?", (recipe_id,)).fetchone()
        if head is None or not head["current_candidate_id"]:
            raise LookupError("usable Recipe not found")
        view = self._checked_view(db, self._candidate(db, head["current_candidate_id"]))
        return {k: view[k] for k in ("recipe_id", "recipe_candidate_id", "version", "recipe_sha256", "recipe")}

    def _register_version(self, db, candidate_id, task_id):
        head = self._head(db, task_id)
        if head is None:
            recipe_id, parent, version = candidate_id, None, 1
            db.execute("INSERT INTO recipe_heads(recipe_id,current_candidate_id,latest_candidate_id) VALUES(?,NULL,?)",
                       (recipe_id, candidate_id))
            db.execute("INSERT INTO task_recipes VALUES(?,?)", (task_id, recipe_id))
        else:
            recipe_id, parent = head["recipe_id"], head["latest_candidate_id"]
            state = db.execute("SELECT state FROM recipe_candidates WHERE id=?", (parent,)).fetchone()["state"]
            if state in ("failed", "awaiting_material"):
                parent = head["current_candidate_id"]
            if not parent:
                raise ValueError("initial Recipe must be recovered before further improvement")
            version = db.execute("SELECT MAX(version)+1 AS version FROM recipe_versions WHERE recipe_id=?",
                                 (recipe_id,)).fetchone()["version"]
            db.execute("UPDATE recipe_heads SET latest_candidate_id=? WHERE recipe_id=?", (candidate_id, recipe_id))
        db.execute("INSERT INTO recipe_versions VALUES(?,?,?,?)", (candidate_id, recipe_id, version, parent))
        return {"recipe_id": recipe_id, "version": version, "parent_candidate_id": parent}

    def _import_recipe(self, db, task_id, view):
        """Start this Task's chain from a Recipe read from the service.

        An uploaded chain continues here under its own ID and version. A generated Recipe starts a new chain without its
        evidence, which refers to the service's records.
        """
        if (not isinstance(view, dict) or view.get("origin") not in ("uploaded", "generated")
                or not isinstance(view.get("recipe_id"), str) or type(view.get("version")) is not int or view["version"] < 1
                or not isinstance(view.get("recipe"), dict)
                or (view["origin"] == "uploaded" and not _RECIPE_ID.fullmatch(view["recipe_id"]))):
            raise ValueError("recipe must be a Recipe returned by get_recipe")
        uploaded = view["origin"] == "uploaded"
        recipe = json.loads(json.dumps(check_upload(view["recipe"]) if uploaded else upload_form(view["recipe"])))
        candidate_id = "recipe_candidate_" + uuid4().hex
        recipe_id, version = (view["recipe_id"], view["version"]) if uploaded else (candidate_id, 1)
        if db.execute("SELECT 1 FROM recipe_heads WHERE recipe_id=?", (recipe_id,)).fetchone() is not None:
            raise ValueError(f"Recipe {recipe_id} is already on this machine; start an execution with its recipe_id")
        context = {"schema": "workflow-recipe-context-v2", "task_id": task_id, "history": [],
                   "memory": {"status": "not_requested", "items": []}, "initial": True,
                   "imported_from": {k: view[k] for k in ("recipe_id", "origin", "version")}}
        db.execute("INSERT INTO recipe_candidates(id,task_id,revision_id,state,phase,context_json,context_sha,recipe_json,recipe_sha,"
                   "publish_state) VALUES(?,?,NULL,'candidate','completed',?,?,?,?,?)",
                   (candidate_id, task_id, canonical_json(context), fingerprint(context), canonical_json(recipe), fingerprint(recipe),
                    "published" if uploaded else None))
        db.execute("INSERT INTO recipe_heads(recipe_id,current_candidate_id,latest_candidate_id,remote_version) VALUES(?,?,?,?)",
                   (recipe_id, candidate_id, candidate_id, version if uploaded else None))
        db.execute("INSERT INTO recipe_versions VALUES(?,?,?,NULL)", (candidate_id, recipe_id, version))
        db.execute("INSERT INTO task_recipes VALUES(?,?)", (task_id, recipe_id))

    def _history(self, db, task_id, revision_id):
        history, current, seen = [], revision_id, set()
        while current:
            if current in seen or len(seen) >= MAX_HISTORY_REVISIONS:
                raise ValueError("invalid or excessive revision history")
            seen.add(current)
            row = db.execute("SELECT r.*,e.request_json,e.kind FROM revisions r JOIN executions e ON e.id=r.execution_id "
                             "WHERE r.id=? AND r.task_id=?", (current, task_id)).fetchone()
            if row is None:
                raise LookupError("task revision history not found")
            execution, outcome = json.loads(row["request_json"]), json.loads(row["result_json"])

            def material(ref):
                item = db.execute("SELECT content,content_hash FROM artifacts WHERE id=?", (ref["artifact_id"],)).fetchone()
                if (item is None or item["content_hash"] != ref["content_hash"]
                        or item["content_hash"] != "sha256:" + fingerprint(item["content"])):
                    raise ValueError("historical artifact unavailable or changed")
                return {**ref, "content": item["content"]}

            history.append({"task_id": task_id, "revision_id": current, "origin": row["kind"], "context": execution["context"],
                            "materials": [material(ref) for ref in execution["context"]["artifacts"]],
                            "outputs": [material(ref) for ref in outcome["artifacts"]], "events": outcome["events"],
                            "trace_coverage": outcome["trace_coverage"], "feedback": self._revision_feedback(db, current)})
            current = execution.get("base_revision_id")
        return list(reversed(history))

    def _existing_candidate(self, db, task_id, revision_id, key, request_hash):
        """Return the replayed or in-flight response; a second request for the same revision reuses that candidate."""
        saved = self._store.replay(db, "recipe_candidate", key, request_hash)
        if saved is not None:
            return saved
        if db.execute("SELECT id FROM revisions WHERE id=? AND task_id=?", (revision_id, task_id)).fetchone() is None:
            raise LookupError("task revision not found")
        placeholders = ",".join("?" * len(_IN_FLIGHT))
        row = db.execute(f"SELECT id,state,context_sha FROM recipe_candidates WHERE task_id=? AND revision_id=? "
                         f"AND state IN ({placeholders}) ORDER BY rowid DESC LIMIT 1", (task_id, revision_id, *_IN_FLIGHT)).fetchone()
        if row is None:
            return None
        lineage = self._lineage(db, row["id"])
        response = {"recipe_candidate_id": row["id"], "revision_id": revision_id, "status": row["state"],
                    "context_sha256": row["context_sha"],
                    **{k: lineage[k] for k in ("recipe_id", "version", "parent_candidate_id")}}
        self._store.remember(db, "recipe_candidate", key, request_hash, response)
        return response

    async def start_recipe_candidate(self, task_id, *, revision_id, key=None, web_search=None, web_search_blocked_domains=()):
        """``web_search`` None searches when the ``LocalRSI`` has a ``web_search``; False never searches; True without
        one fails the candidate with ``web_search_unavailable``."""
        blocked = list(dict.fromkeys(self._web_blocked + validate_blocked_domains(web_search_blocked_domains)))
        if blocked and web_search is False:
            raise ValueError("web_search_blocked_domains needs web search")
        key = stable_key(recipe_request_key(task_id, revision_id, web_search=web_search, web_search_blocked_domains=blocked)
                         if key is None else key)
        request_hash = fingerprint({"task_id": task_id, "revision_id": revision_id,
                                    **({"web_search": web_search} if web_search is not None else {}),
                                    **({"web_search_blocked_domains": blocked} if blocked else {})})
        searching = web_search if web_search is not None else self._web_search is not None
        with self._store.transaction() as db:
            found = self._existing_candidate(db, task_id, revision_id, key, request_hash)
            if found is not None:
                return result(found)
            source = json.loads(db.execute("SELECT e.request_json FROM revisions r JOIN executions e ON e.id=r.execution_id "
                                           "WHERE r.id=?", (revision_id,)).fetchone()["request_json"])
        # Memory reaches a Recipe only when the execution behind this revision asked for it.
        memory = await self._recall_snapshot(source["context"]["objective"]) if "memory" in source else {
            "status": "not_requested", "items": []}
        with self._store.transaction() as db:
            found = self._existing_candidate(db, task_id, revision_id, key, request_hash)
            if found is not None:
                return result(found)
            context = {"schema": "workflow-recipe-context-v2", "task_id": task_id, "source_revision_id": revision_id,
                       "history": self._history(db, task_id, revision_id), "memory": memory,
                       "confirmation_source": "explicit_generation_request"}
            if searching:
                context["model_settings"] = {"web_search": {"blocked_domains": blocked}}
            if len(canonical_json(context).encode()) > MAX_CONTEXT_BYTES:
                raise ValueError("recipe history exceeds 4 MB; narrow the context")
            candidate_id = "recipe_candidate_" + uuid4().hex
            context["initial"] = self._head(db, task_id) is None
            context_sha = fingerprint(context)
            db.execute("INSERT INTO recipe_candidates(id,task_id,revision_id,state,context_json,context_sha) VALUES(?,?,?,'queued',?,?)",
                       (candidate_id, task_id, revision_id, canonical_json(context), context_sha))
            lineage = self._register_version(db, candidate_id, task_id)
            response = {"recipe_candidate_id": candidate_id, "revision_id": revision_id, "status": "queued",
                        "context_sha256": context_sha, **lineage}
            self._store.remember(db, "recipe_candidate", key, request_hash, response)
        return result(response)

    def _claim(self, candidate_id):
        """Decide the next step for this candidate; only one caller can move it to running."""
        with self._store.transaction() as db:
            row = self._candidate(db, candidate_id)
            if row["state"] == "running":
                if row["lease_expires_at"] > time.time():
                    return {"action": "wait"}
                db.execute("UPDATE recipe_candidates SET state='interrupted',error=?,lease_expires_at=NULL WHERE id=?",
                           (canonical_json(INTERRUPTED), candidate_id))
                return {"action": "done"}
            if row["state"] not in ("queued", "awaiting_parent"):
                return {"action": "done"}
            parent_id = self._lineage(db, candidate_id)["parent_candidate_id"]
            parent_state = db.execute("SELECT state FROM recipe_candidates WHERE id=?", (parent_id,)).fetchone()["state"] if parent_id else None
            if parent_state in _IN_FLIGHT:
                return {"action": "settle_parent", "parent": parent_id}
            if parent_state in ("failed", "interrupted"):
                return {"action": "wait"}  # blocked until the parent is resumed
            lease = time.time() + self._model.timeout * _STAGES * MODEL_OUTPUT_ATTEMPTS
            db.execute("UPDATE recipe_candidates SET state='running',lease_expires_at=? WHERE id=?", (lease, candidate_id))
            return {"action": "generate", "row": dict(row), "parent_id": parent_id}

    def _save_attribution(self, row, analysis, models, web=None):
        with self._store.transaction() as db:
            db.execute("UPDATE recipe_candidates SET attribution_json=? WHERE id=? AND state='running' AND generation=?",
                       (canonical_json({**analysis, "models": models, **({"web": web} if web else {})}),
                        row["id"], row["generation"]))

    def _set_phase(self, candidate_id, generation, phase):
        with self._store.transaction() as db:
            db.execute("UPDATE recipe_candidates SET phase=? WHERE id=? AND state='running' AND generation=?",
                       (phase, candidate_id, generation))

    def _commit_recipe(self, row, parent, stages):
        recipe = stages.recipe
        record = canonical_json({**stages.analysis, "models": stages.models, **({"web": stages.web} if stages.web else {})})
        with self._store.transaction() as db:
            lineage = self._lineage(db, row["id"])
            if lineage["current_candidate_id"] != lineage["parent_candidate_id"]:
                raise ParentChanged("Recipe parent is no longer current")
            if parent and self._candidate(db, parent["recipe_candidate_id"])["recipe_sha"] != parent["recipe_sha256"]:
                raise ParentChanged("parent Recipe changed during generation")
            updated = db.execute(
                "UPDATE recipe_candidates SET phase='completed',state='candidate',recipe_json=?,recipe_sha=?,attribution_json=?,"
                "lease_expires_at=NULL,publish_state=? WHERE id=? AND state='running' AND generation=?",
                (canonical_json(recipe), fingerprint(recipe), record, "pending" if self._atoms is not None else None,
                 row["id"], row["generation"]))
            if updated.rowcount != 1:
                raise ParentChanged("candidate is no longer being generated by this caller")
            db.execute("UPDATE recipe_heads SET current_candidate_id=? WHERE recipe_id=?", (row["id"], lineage["recipe_id"]))

    def _keep_previous(self, row, stages):
        context = {**json.loads(row["context_json"]), "material_gap": stages.material_gap}
        with self._store.transaction() as db:
            lineage = self._lineage(db, row["id"])
            updated = db.execute(
                "UPDATE recipe_candidates SET state='awaiting_material',phase='completed',attribution_json=?,context_json=?,"
                "context_sha=?,lease_expires_at=NULL WHERE id=? AND state='running' AND generation=?",
                (canonical_json({**stages.analysis, "models": stages.models}), canonical_json(context), fingerprint(context),
                 row["id"], row["generation"]))
            if updated.rowcount == 1:
                db.execute("UPDATE recipe_versions SET parent_candidate_id=? WHERE parent_candidate_id=?",
                           (lineage["parent_candidate_id"], row["id"]))

    def _record_failure(self, row, exc, phase):
        with self._store.transaction() as db:
            updated = db.execute("UPDATE recipe_candidates SET state='failed',error=?,lease_expires_at=NULL "
                                 "WHERE id=? AND state='running' AND generation=?",
                                 (canonical_json(_failure_detail(exc, phase)), row["id"], row["generation"]))
            if updated.rowcount == 1:
                db.execute("WITH RECURSIVE descendants AS (SELECT candidate_id FROM recipe_versions WHERE parent_candidate_id=? "
                           "UNION ALL SELECT v.candidate_id FROM recipe_versions v JOIN descendants d ON v.parent_candidate_id=d.candidate_id) "
                           "UPDATE recipe_candidates SET state='failed',error=? "
                           "WHERE id IN (SELECT candidate_id FROM descendants) AND state IN ('queued','awaiting_parent')",
                           (row["id"], canonical_json(PARENT_FAILED)))

    def _requeue(self, row):
        with self._store.transaction() as db:
            db.execute("UPDATE recipe_candidates SET state='queued',phase='queued',lease_expires_at=NULL "
                       "WHERE id=? AND state='running' AND generation=?", (row["id"], row["generation"]))

    async def _generate(self, row, parent_id):
        phases = ["preparing"]

        def on_phase(phase):
            phases.append(phase)
            self._set_phase(row["id"], row["generation"], phase)

        try:
            context = json.loads(row["context_json"])
            if fingerprint(context) != row["context_sha"]:
                raise ValueError("recipe context changed")
            with self._store.reading() as db:
                parent = self._checked_view(db, self._candidate(db, parent_id)) if parent_id else None
            web = None
            settings = context.get("model_settings", {}).get("web_search")
            if settings is not None:
                saved = json.loads(row["attribution_json"]) if row["attribution_json"] else {}
                web = WebPlan(self._web_search, tuple(settings["blocked_domains"]), saved.get("web"))
            stages = await run_recipe_stages(
                self._model, context, parent, on_phase=on_phase, web=web,
                on_attribution=lambda analysis, models: self._save_attribution(row, analysis, models),
                on_web=lambda analysis, models, frozen: self._save_attribution(row, analysis, models, frozen))
            if stages.recipe is None:
                self._keep_previous(row, stages)
                return
            phases.append("committing")
            self._commit_recipe(row, parent, stages)
        except asyncio.CancelledError:
            self._requeue(row)  # nothing was committed, so the next wait starts over
            raise
        except Exception as exc:  # noqa: BLE001 -- recorded as the candidate's failure
            _LOG.exception("Recipe candidate %s failed in phase %s", row["id"], phases[-1])
            self._record_failure(row, exc, phases[-1])

    async def _settle(self, candidate_id):
        delay = 0.2
        while True:
            step = self._claim(candidate_id)
            if step["action"] == "done":
                return
            if step["action"] == "generate":
                await self._generate(step["row"], step["parent_id"])
            elif step["action"] == "settle_parent":
                await self._settle(step["parent"])
            else:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    def _settling_task(self, candidate_id):
        """One generation per candidate in this process; a later wait attaches to it."""
        task = self._settling.get(candidate_id)
        if task is None or task.done():
            task = asyncio.create_task(self._settle(candidate_id))
            self._settling[candidate_id] = task
            task.add_done_callback(lambda _: self._settling.pop(candidate_id, None))
        return task

    async def wait_workflow_job(self, kind, job_id, *, timeout=300):
        """Generate the candidate in this process. A timeout leaves it running; wait on the same ID to continue."""
        if kind != "recipe_candidate":
            raise ValueError("job kind must be recipe_candidate")
        timeout = _job_timeout(timeout)
        job = await self.get_recipe_candidate(job_id)
        if job["status"] in _IN_FLIGHT:
            task = self._settling_task(job_id)
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise TimeoutError(f"Workflow {kind} {job_id} has not settled; wait on the same ID to continue")
            task.result()
            job = await self.get_recipe_candidate(job_id)
        if job["status"] == "failed":
            raise RSICandidateFailed.from_candidate(job)
        if self._atoms is not None and job["current"] and job["publish_state"] == "pending":  # also recovers a publish lost to a crash
            await self._publish_candidate(job_id)
            job = await self.get_recipe_candidate(job_id)
        return job

    async def get_recipe_candidate(self, recipe_id):
        with self._store.reading() as db:
            return result(self._checked_view(db, self._candidate(db, recipe_id)))

    async def list_recipe_candidates(self, *, limit=20, cursor=None, task_id=None, status=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100")
        if status is not None and status not in _LIST_STATES:
            raise ValueError("invalid Recipe status")
        conditions, parameters = ["1=1"], []
        with self._store.reading() as db:
            if cursor is not None:
                previous = db.execute("SELECT rowid AS position FROM recipe_candidates WHERE id=?", (cursor,)).fetchone()
                if previous is None:
                    raise ValueError("invalid Recipe cursor")
                conditions.append("rowid<?")
                parameters.append(previous["position"])
            if task_id is not None:
                conditions.append("task_id=?")
                parameters.append(task_id)
            if status is not None:
                conditions.append("state=?")
                parameters.append(status)
            rows = db.execute("SELECT * FROM recipe_candidates WHERE " + " AND ".join(conditions) + " ORDER BY rowid DESC LIMIT ?",
                              (*parameters, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                item = self._view(db, row)
                recipe = item.pop("recipe")
                item["title"] = recipe["title"] if recipe else None
                items.append(item)
        has_more = len(rows) > limit
        return result({"items": items, "has_more": has_more, "next_cursor": items[-1]["recipe_candidate_id"] if has_more else None})

    async def resume_recipe_candidate(self, recipe_id):
        with self._store.transaction() as db:
            row = self._candidate(db, recipe_id)
            if row["state"] in ("queued", "awaiting_parent", "running", "candidate"):
                return result(self._checked_view(db, row))  # a retry of a resume whose response was lost
            if row["state"] not in ("failed", "interrupted"):
                raise ValueError("only a failed or interrupted candidate can resume")
            parent_id = self._lineage(db, recipe_id)["parent_candidate_id"]
            ready = parent_id is None or self._candidate(db, parent_id)["state"] == "candidate"
            state = "queued" if ready else "awaiting_parent"
            db.execute("UPDATE recipe_candidates SET state=?,phase='queued',error=NULL,lease_expires_at=NULL,"
                       "generation=generation+1 WHERE id=?", (state, recipe_id))
        return result({"recipe_candidate_id": recipe_id, "status": state})

    async def get_task_recipe(self, task_id):
        with self._store.reading() as db:
            if db.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
                raise LookupError("task not found")
            head = self._head(db, task_id)
            if head is None:
                return result({"task_id": task_id, "status": "awaiting_execution", "recipe_id": None, "current": None, "latest": None})
            current = self._checked_view(db, self._candidate(db, head["current_candidate_id"])) if head["current_candidate_id"] else None
            latest = self._checked_view(db, self._candidate(db, head["latest_candidate_id"]))
        return result({"task_id": task_id, "recipe_id": head["recipe_id"], "status": latest["status"], "current": current, "latest": latest})

    async def get_recipe_versions(self, recipe_id):
        with self._store.reading() as db:
            head = db.execute("SELECT * FROM recipe_heads WHERE recipe_id=?", (recipe_id,)).fetchone()
            if head is None:
                raise LookupError("Recipe not found")
            rows = db.execute("SELECT candidate_id FROM recipe_versions WHERE recipe_id=? ORDER BY version", (recipe_id,)).fetchall()
            versions = [self._checked_view(db, self._candidate(db, r["candidate_id"])) for r in rows]
        return result({"recipe_id": recipe_id, "current_candidate_id": head["current_candidate_id"],
                       "latest_candidate_id": head["latest_candidate_id"], "versions": versions})

    async def _publish_candidate(self, candidate_id):
        """Upload this version to the service's Recipe store. The local version stands whatever happens here."""
        with self._store.reading() as db:
            row = self._candidate(db, candidate_id)
            lineage = self._lineage(db, candidate_id)
            head = db.execute("SELECT remote_version FROM recipe_heads WHERE recipe_id=?", (lineage["recipe_id"],)).fetchone()
        recipe_id, recipe, remote = lineage["recipe_id"], upload_form(json.loads(row["recipe_json"])), head["remote_version"]
        try:
            # base_version is the last version the server is known to have, so a version uploaded elsewhere is a conflict.
            try:
                await self._atoms.upload_recipe(recipe_id, version=lineage["version"], recipe=recipe, base_version=remote)
            except RSIHTTPError as exc:
                ours = await self._own_server_version(recipe_id, exc, remote)
                if ours is None:
                    raise
                remote = ours
                await self._atoms.upload_recipe(recipe_id, version=lineage["version"], recipe=recipe, base_version=remote)
            state = "published"
        except RSIHTTPError as exc:
            state = "publish_conflict" if exc.status_code == 409 else "failed"
            _LOG.warning("Uploading Recipe candidate %s returned HTTP %s", candidate_id, exc.status_code)
        except RSIError as exc:
            state = "failed"
            _LOG.warning("Uploading Recipe candidate %s failed: %s", candidate_id, type(exc).__name__)
        with self._store.transaction() as db:
            db.execute("UPDATE recipe_candidates SET publish_state=? WHERE id=?", (state, candidate_id))
            db.execute("UPDATE recipe_heads SET remote_version=? WHERE recipe_id=?",
                       (lineage["version"] if state == "published" else remote, recipe_id))
        return state

    async def _own_server_version(self, recipe_id, exc, remote):
        """The server's newer current version when it is this chain's own upload whose response was lost, else None."""
        current = exc.current_version
        if exc.status_code != 409 or current is None or current <= (remote or 0):
            return None
        stored = (await self._atoms.get_recipe(recipe_id, version=current)).get("recipe")
        with self._store.reading() as db:
            local = db.execute("SELECT c.recipe_json FROM recipe_versions v JOIN recipe_candidates c ON c.id=v.candidate_id "
                               "WHERE v.recipe_id=? AND v.version=?", (recipe_id, current)).fetchone()
        if local is None or not local["recipe_json"] or not isinstance(stored, dict):
            return None
        same = fingerprint(without_evidence(stored)) == fingerprint(upload_form(json.loads(local["recipe_json"])))
        return current if same else None
