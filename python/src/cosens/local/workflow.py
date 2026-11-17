"""Tasks, executions, revisions and feedback, with the same requests and responses as ``client.atoms``."""

import json
from uuid import uuid4

from ..atoms_helpers import stable_key
from ..models import result
from ..workflow_atoms import _INPUT_ROLES, _context, _input, _result, _task, feedback_source, validate_artifact
from .recipe_stages import MEMORY_TOKEN_BUDGET, memory_snapshot
from .store import canonical_json, fingerprint

# A task's own artifacts are reference material; an execution's input takes the four input roles.
_TASK_ROLES = {"reference"}
_INPUT_OUTPUT = {"original_input", "submitted_output"}
# Learning states in the service's words: a finished candidate is a completed learning, and one waiting for the version
# it builds on awaits the previous version.
_LEARNING_STATUS = {"candidate": "completed", "awaiting_parent": "awaiting_previous_version"}
# How the host produces an execution: from the input, or by reconstructing an output that already exists.
_SOURCES = {"new_task": "original", "external_artifact": "reconstructed"}


def _published_recipe(text):
    """True for a Recipe that SDK 0.6 published to Memory, which may still be there."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("schema") == "cosens-recipe/v1"


def _input_missing(value, kind):
    """What a new execution's input lacks; a reconstruction also needs what it started from (``_input`` already
    refused one without its submitted output)."""
    missing = [] if value["objective"] else ["objective"]
    if kind == "reconstructed" and not value["self_contained"]:
        if "original_input" not in {a["role"] for a in value["artifacts"]}:
            missing.append("original_input_or_self_contained_task")
    return missing


class LocalTaskWorkflow:
    def _artifact_refs(self, db, values, *, roles, default_role=None):
        refs, seen = [], set()
        for item in values:
            if isinstance(item, str) and default_role:
                item = {"artifact_id": item, "role": default_role}
            if not isinstance(item, dict) or set(item) != {"artifact_id", "role"} or item["role"] not in roles:
                raise ValueError("invalid artifact reference or role")
            artifact_id, role = item["artifact_id"], item["role"]
            if (artifact_id, role) in seen:
                raise ValueError("duplicate artifact reference")
            if role in _INPUT_OUTPUT and any(a == artifact_id and r != role and r in _INPUT_OUTPUT for a, r in seen):
                raise ValueError("original input and submitted output cannot share an artifact")
            seen.add((artifact_id, role))
            row = db.execute("SELECT name,content_hash FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if row is None:
                raise LookupError("artifact unavailable")
            refs.append({"artifact_id": artifact_id, "role": role, "name": row["name"], "content_hash": row["content_hash"]})
        return refs

    async def artifact(self, *, name, content):
        validate_artifact(name, content)
        artifact_id, content_hash = "artifact_" + uuid4().hex, "sha256:" + fingerprint(content)
        with self._store.transaction() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?)", (artifact_id, name[:256], content, content_hash))
        return result({"artifact_id": artifact_id, "name": name[:256], "content_hash": content_hash,
                       "size_bytes": len(content.encode())})

    async def get_artifact(self, artifact_id):
        row = self._store.read_one("SELECT name,content,content_hash FROM artifacts WHERE id=?", (artifact_id,))
        if row is None:
            raise LookupError("artifact not found")
        if row["content_hash"] != "sha256:" + fingerprint(row["content"]):
            raise ValueError("artifact content changed")
        return result({"artifact_id": artifact_id, "name": row["name"], "content": row["content"],
                       "content_hash": row["content_hash"]})

    async def create_task(self, *, key, objective, artifacts=None, recipe=None):
        key, body = stable_key(key), _task(objective, artifacts)
        request_hash = fingerprint({**body, **({"recipe": recipe} if recipe is not None else {})})
        with self._store.transaction() as db:
            saved = self._store.replay(db, "create_task", key, request_hash)
            if saved is not None:
                return result(saved)
            context = {"objective": objective,
                       "artifacts": self._artifact_refs(db, body["artifacts"], roles=_TASK_ROLES, default_role="reference")}
            task_id = "task_" + uuid4().hex
            db.execute("INSERT INTO tasks VALUES(?,?,1)", (task_id, canonical_json(context)))
            if recipe is not None:
                self._import_recipe(db, task_id, recipe)
            response = {"task_id": task_id, **context, "context_version": 1}
            self._store.remember(db, "create_task", key, request_hash, response)
        return result(response)

    @staticmethod
    def _task_view(db, row):
        context = json.loads(row["context_json"])
        executions = [dict(r) for r in db.execute(
            "SELECT id AS execution_id,instance_id,kind,status,request_sha AS request_sha256 FROM executions "
            "WHERE task_id=? ORDER BY rowid", (row["id"],))]
        # The head's latest version is the newest learning on the Recipe, in flight or finished; a version that was
        # not learned from an execution (imported) has no learning.
        recipes = [{"recipe_id": r["recipe_id"], "current_version": r["current_version"],
                    "latest_version": r["latest_version"],
                    "learning": {"execution_id": r["execution_id"], "version": r["latest_version"],
                                 "status": _LEARNING_STATUS.get(r["state"], r["state"]), "phase": r["phase"],
                                 "error": json.loads(r["error"])["code"] if r["error"] else None}
                    if r["execution_id"] else None}
                   for r in db.execute(
                       "SELECT t.recipe_id,cv.version AS current_version,lv.version AS latest_version,"
                       "r.execution_id,c.state,c.phase,c.error FROM task_recipes t "
                       "JOIN recipe_heads h ON h.recipe_id=t.recipe_id "
                       "JOIN recipe_versions lv ON lv.candidate_id=h.latest_candidate_id "
                       "JOIN recipe_candidates c ON c.id=h.latest_candidate_id "
                       "LEFT JOIN recipe_versions cv ON cv.candidate_id=h.current_candidate_id "
                       "LEFT JOIN revisions r ON r.id=c.revision_id "
                       "WHERE t.task_id=? ORDER BY t.recipe_id", (row["id"],))]
        return {"task_id": row["id"], "objective": context["objective"], "artifacts": context["artifacts"],
                "context_version": row["context_version"], "executions": executions, "recipes": recipes}

    async def get_task(self, task_id):
        with self._store.reading() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise LookupError("task not found")
            return result(self._task_view(db, row))

    async def list_tasks(self, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        condition, parameters = "", []
        with self._store.reading() as db:
            if cursor:
                row = db.execute("SELECT rowid AS position FROM tasks WHERE id=?", (cursor,)).fetchone()
                if row is None:
                    raise ValueError("invalid task cursor")
                condition, parameters = "WHERE rowid<?", [row["position"]]
            rows = db.execute(f"SELECT * FROM tasks {condition} ORDER BY rowid DESC LIMIT ?",
                              (*parameters, limit + 1)).fetchall()
            items = [self._task_view(db, row) for row in rows[:limit]]
        return result({"items": items, "has_more": len(rows) > limit,
                       "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    async def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None):
        body = _context(expected_context_version, objective, artifacts)
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise LookupError("task not found")
            if expected_context_version != row["context_version"]:
                raise ValueError("context version changed")
            context = json.loads(row["context_json"])
            if "objective" in body:
                context["objective"] = objective
            if "artifacts" in body:
                additions = self._artifact_refs(db, artifacts, roles=_TASK_ROLES, default_role="reference")
                existing = {a["artifact_id"] for a in context["artifacts"]}
                if any(a["artifact_id"] in existing for a in additions):
                    raise ValueError("artifact already attached")
                context["artifacts"].extend(additions)
                if len(context["artifacts"]) > 100:
                    raise ValueError("too many artifacts")
            db.execute("UPDATE tasks SET context_json=?,context_version=? WHERE id=?",
                       (canonical_json(context), row["context_version"] + 1, task_id))
        return await self.get_task(task_id)

    async def _recall_snapshot(self, objective):
        recalled = await self._atoms.memory_recall(query=objective, token_budget=MEMORY_TOKEN_BUDGET)
        items = json.loads(json.dumps(recalled.get("items", [])))
        # A published Recipe is a method, not evidence about the task.
        return memory_snapshot([item for item in items if not _published_recipe(item.get("text"))])

    @staticmethod
    def _start_record(db, key, operation):
        """The receipt ``key`` left under ``operation``; a key that another execution start took is refused.

        New executions record under "start_execution"; a revision records under its revision, as the service's
        corrections do.
        """
        rows = db.execute("SELECT operation,request_hash,response_json FROM idempotency WHERE request_key=? "
                          "AND (operation='start_execution' OR operation LIKE 'start_revision:%') ORDER BY rowid",
                          (key,)).fetchall()
        for row in rows:
            if row["operation"] == operation:
                return row
        if rows:
            raise ValueError("idempotency key reused with different execution")
        return None

    def _execution_intent(self, db, task_id, key, source, body, recipe_id, recall_memory):
        """Return the replayed response, or the frozen request to create; refuses an incomplete input."""
        kind = _SOURCES[source]
        value = {"objective": body.get("objective"), "self_contained": body.get("self_contained", False),
                 "artifacts": self._artifact_refs(db, body["artifacts"], roles=_INPUT_ROLES,
                                                  default_role="original_input" if kind == "original" else "submitted_output")}
        missing = _input_missing(value, kind)
        if missing:
            raise ValueError(f"the execution input is incomplete: {', '.join(missing)}")
        intent = fingerprint({"task_id": task_id, "kind": kind, "input": value,
                              **({"requested_recipe_id": recipe_id} if recipe_id else {}),
                              **({"recall_memory": True} if recall_memory else {})})
        # One operation for every new execution: the intent covers what the caller sent (task, source, input), so a key
        # resent with any of them changed is refused instead of starting a second execution. It leaves out the task's
        # context, which a supplement may change before a retry, so replay is resolved before that row is read.
        saved = self._start_record(db, key, "start_execution")
        if saved is not None:
            if saved["request_hash"] != intent:
                raise ValueError("idempotency key reused with different execution")
            return {"response": json.loads(saved["response_json"])}
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise LookupError("task not found")
        request = {"task_id": task_id, "kind": kind, "trace_origin": kind,
                   "task_context": {**json.loads(row["context_json"]), "context_version": row["context_version"]},
                   "input": value}
        return {"request": request, "intent": intent, "kind": kind}

    async def start_execution(self, task_id, *, key, source, objective=None, artifacts=None, self_contained=None,
                              recipe_id=None, recall_memory=False):
        key = stable_key(key)
        if type(recall_memory) is not bool:
            raise ValueError("recall_memory must be boolean")
        body = _input(source, objective, artifacts, self_contained)
        if recall_memory and self._atoms is None:
            raise ValueError("recall_memory requires a client")
        with self._store.transaction() as db:
            first = self._execution_intent(db, task_id, key, source, body, recipe_id, recall_memory)
        if "response" in first:
            return result(first["response"])
        # Recall runs outside the transaction, so a concurrent caller with this key is re-checked below.
        memory = await self._recall_snapshot(first["request"]["input"]["objective"]) if recall_memory else None
        with self._store.transaction() as db:
            intent = self._execution_intent(db, task_id, key, source, body, recipe_id, recall_memory)
            if "response" in intent:
                return result(intent["response"])
            if intent["request"]["task_context"] != first["request"]["task_context"]:
                raise ValueError("task context changed while recalling Memory; start the execution again")
            request = intent["request"]
            method = self._execution_method(db, task_id, recipe_id)
            if method:
                request["method"] = method
            if memory is not None:
                request["memory"] = memory
            request_sha, execution_id = fingerprint(request), "execution_" + uuid4().hex
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,NULL,?)",
                       (execution_id, task_id, intent["kind"], "awaiting_execution", canonical_json(request), request_sha,
                        execution_id))
            response = {"task_id": task_id, "execution_id": execution_id, "instance_id": execution_id,
                        "kind": intent["kind"], "status": "awaiting_execution", "request_sha256": request_sha, "request": request}
            self._store.remember(db, "start_execution", key, intent["intent"], response)
        return result(response)

    async def get_execution(self, execution_id):
        row = self._store.read_one("SELECT * FROM executions WHERE id=?", (execution_id,))
        if row is None:
            raise LookupError("execution not found")
        saved = json.loads(row["result_json"]) if row["result_json"] else None
        return result({"execution_id": execution_id, "task_id": row["task_id"], "instance_id": row["instance_id"],
                       "kind": row["kind"], "status": row["status"],
                       "request_sha256": row["request_sha"], "request": json.loads(row["request_json"]),
                       "result": saved["response"] if saved else None})

    async def submit_execution(self, execution_id, *, request_sha256, outcome, artifacts, events=None, trace_coverage="unknown"):
        body = _result(request_sha256, outcome, artifacts, events if events is not None else [], trace_coverage)
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                raise LookupError("execution not found")
            if request_sha256 != row["request_sha"]:
                raise ValueError("execution request hash mismatch")
            if row["result_json"]:
                saved = json.loads(row["result_json"])
                if saved["submitted"] != body:
                    raise ValueError("execution result already frozen")
                return result(saved["response"])
            refs = self._artifact_refs(db, artifacts, roles=_INPUT_ROLES)
            response = {"task_id": row["task_id"], "execution_id": execution_id, "status": outcome, "revision_id": None,
                        "trace_origin": row["kind"]}
            if outcome == "completed":
                revision_id = "revision_" + uuid4().hex
                response["revision_id"] = revision_id
                revision_result = {"artifacts": refs, "events": body["events"], "trace_coverage": trace_coverage,
                                   "trace_coverage_attestation": "host_reported_unverified"}
                db.execute("INSERT INTO revisions VALUES(?,?,?,?)",
                           (revision_id, row["task_id"], execution_id, canonical_json(revision_result)))
            db.execute("UPDATE executions SET status=?,result_json=? WHERE id=?",
                       (outcome, canonical_json({"submitted": body, "response": response}), execution_id))
        return result(response)

    async def get_revision(self, revision_id):
        row = self._store.read_one(
            "SELECT r.*,e.kind,e.request_sha FROM revisions r JOIN executions e ON e.id=r.execution_id WHERE r.id=?",
            (revision_id,))
        if row is None:
            raise LookupError("revision not found")
        return result({"revision_id": revision_id, "task_id": row["task_id"], "execution_id": row["execution_id"],
                       "trace_origin": row["kind"], "request_sha256": row["request_sha"],
                       "result": json.loads(row["result_json"])})

    @staticmethod
    def _revision_feedback(db, revision_id):
        return [json.loads(row["body_json"]) for row in db.execute(
            "SELECT body_json FROM feedback WHERE revision_id=? ORDER BY rowid", (revision_id,))]

    async def record_feedback(self, revision_id, *, feedback, source, key):
        feedback_source(source)
        key = stable_key(key)
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 16_000:
            raise ValueError("feedback must be nonempty text up to 16000 characters")
        with self._store.transaction() as db:
            row = db.execute("SELECT task_id FROM revisions WHERE id=?", (revision_id,)).fetchone()
            if row is None:
                raise LookupError("revision not found")
            old = db.execute("SELECT body_json FROM feedback WHERE request_key=?", (key,)).fetchone()
            if old is not None:
                value = json.loads(old["body_json"])
                if (value["revision_id"], value["message"], value["source"]) != (revision_id, feedback, source):
                    raise ValueError("idempotency key reused with different feedback")
                return result(value)
            value = {"feedback_id": "feedback_" + uuid4().hex, "revision_id": revision_id, "task_id": row["task_id"],
                     "message": feedback, "source": source}
            db.execute("INSERT INTO feedback VALUES(?,?,?,?)", (value["feedback_id"], revision_id, key, canonical_json(value)))
        return result(value)

    async def start_revision(self, revision_id, *, key):
        key = stable_key(key)
        operation = "start_revision:" + revision_id
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT r.*,e.request_json,e.instance_id FROM revisions r JOIN executions e ON e.id=r.execution_id "
                "WHERE r.id=?",
                (revision_id,)).fetchone()
            if row is None:
                raise LookupError("revision not found")
            # Resolve replay before reading mutable feedback or the current Recipe.
            old = self._start_record(db, key, operation)
            if old is not None:
                return result(json.loads(old["response_json"]))
            previous = json.loads(row["request_json"])
            request = {"task_id": row["task_id"], "kind": "revision", "trace_origin": "revision",
                       "task_context": previous["task_context"], "input": previous["input"], "base_revision_id": revision_id,
                       "base_result": json.loads(row["result_json"]), "feedback": self._revision_feedback(db, revision_id)}
            method = self._correction_method(db, row["task_id"], previous)
            if method:
                request["method"] = method
            digest, execution_id = fingerprint(request), "execution_" + uuid4().hex
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,NULL,?)",
                       (execution_id, row["task_id"], "revision", "awaiting_execution", canonical_json(request), digest,
                        row["instance_id"]))
            response = {"task_id": row["task_id"], "execution_id": execution_id, "instance_id": row["instance_id"],
                        "kind": "revision", "status": "awaiting_execution", "request_sha256": digest, "request": request}
            self._store.remember(db, operation, key, digest, response)
        return result(response)
