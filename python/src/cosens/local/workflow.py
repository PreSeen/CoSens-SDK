"""Tasks, executions, revisions and feedback, with the same requests and responses as ``client.atoms``."""

import json
from uuid import uuid4

from ..atoms_helpers import stable_key
from ..models import result
from ..workflow_atoms import _context, _result, _task, feedback_source, validate_artifact
from .recipe_stages import MEMORY_TOKEN_BUDGET, memory_snapshot
from .store import canonical_json, fingerprint

_ARTIFACT_ROLES = {"original_input", "submitted_output", "acceptance", "external_trace"}
_INPUT_OUTPUT = {"original_input", "submitted_output"}


def _published_recipe(text):
    """True for a Recipe that SDK 0.6 published to Memory, which may still be there."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get("schema") == "cosens-recipe/v1"


def _missing(context, mode):
    missing = []
    if not context.get("objective"):
        missing.append("objective")
    if (mode == "reconstruct" and not any(a["role"] == "original_input" for a in context["artifacts"])
            and not context.get("self_contained")):
        missing.append("original_input_or_self_contained_task")
    return missing


class LocalTaskWorkflow:
    def _artifact_refs(self, db, values, *, default_role=None):
        refs, seen = [], set()
        for item in values:
            if isinstance(item, str) and default_role:
                item = {"artifact_id": item, "role": default_role}
            if not isinstance(item, dict) or set(item) != {"artifact_id", "role"} or item["role"] not in _ARTIFACT_ROLES:
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

    async def create_task(self, *, source, key, objective=None, artifacts=None, self_contained=None, recipe=None):
        key, body = stable_key(key), _task(source, objective, artifacts, self_contained)
        request_hash = fingerprint({**body, **({"recipe": recipe} if recipe is not None else {})})
        mode = {"new_task": "execute", "external_artifact": "reconstruct"}[source]
        with self._store.transaction() as db:
            saved = self._store.replay(db, "create_task", key, request_hash)
            if saved is not None:
                return result(saved)
            refs = self._artifact_refs(db, body["artifacts"],
                                       default_role="original_input" if mode == "execute" else "submitted_output")
            context = {"objective": objective, "self_contained": body.get("self_contained", False), "artifacts": refs}
            task_id = "task_" + uuid4().hex
            db.execute("INSERT INTO tasks VALUES(?,?,?,1)", (task_id, mode, canonical_json(context)))
            if recipe is not None:
                self._import_recipe(db, task_id, recipe)
            missing = _missing(context, mode)
            response = {"task_id": task_id, "source": source, "status": "needs_context" if missing else "ready",
                        "missing": missing, "context_version": 1}
            self._store.remember(db, "create_task", key, request_hash, response)
        return result(response)

    def _task_view(self, row):
        context = json.loads(row["context_json"])
        executions = [dict(r) for r in self._store.read(
            "SELECT id,kind,status,request_sha FROM executions WHERE task_id=? ORDER BY rowid", (row["id"],))]
        missing = _missing(context, row["mode"])
        return {"task_id": row["id"], "source": "new_task" if row["mode"] == "execute" else "external_artifact",
                "mode": row["mode"], "context": context, "context_version": row["context_version"],
                "status": "needs_context" if missing else "ready", "missing": missing, "executions": executions}

    async def get_task(self, task_id):
        row = self._store.read_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise LookupError("task not found")
        return result(self._task_view(row))

    async def list_tasks(self, *, limit=20, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        condition, parameters = "", []
        if cursor:
            row = self._store.read_one("SELECT rowid AS position FROM tasks WHERE id=?", (cursor,))
            if row is None:
                raise ValueError("invalid task cursor")
            condition, parameters = "WHERE rowid<?", [row["position"]]
        rows = self._store.read(f"SELECT * FROM tasks {condition} ORDER BY rowid DESC LIMIT ?", (*parameters, limit + 1))
        return result({"items": [self._task_view(row) for row in rows[:limit]], "has_more": len(rows) > limit,
                       "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None})

    async def supplement_task_context(self, task_id, *, expected_context_version, objective=None, artifacts=None,
                                      self_contained=None):
        body = _context(expected_context_version, objective, artifacts, self_contained)
        with self._store.transaction() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise LookupError("task not found")
            if expected_context_version != row["context_version"]:
                raise ValueError("context version changed")
            context = json.loads(row["context_json"])
            if "objective" in body:
                if not isinstance(objective, str) or not objective.strip() or len(objective) > 20_000:
                    raise ValueError("invalid objective")
                context["objective"] = objective
            if "self_contained" in body:
                if type(self_contained) is not bool:
                    raise ValueError("self_contained must be boolean")
                context["self_contained"] = self_contained
            if "artifacts" in body:
                additions = self._artifact_refs(db, artifacts)
                existing = {(a["artifact_id"], a["role"]) for a in context["artifacts"]}
                if any((a["artifact_id"], a["role"]) in existing for a in additions):
                    raise ValueError("artifact already attached")
                opposite = {"original_input": "submitted_output", "submitted_output": "original_input"}
                if any((a["artifact_id"], opposite[a["role"]]) in existing for a in additions if a["role"] in opposite):
                    raise ValueError("original input and submitted output cannot share an artifact")
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

    def _execution_intent(self, db, task_id, key, recipe_id, recall_memory):
        """Return the replayed or needs-context response, or the frozen request to create."""
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise LookupError("task not found")
        context = json.loads(row["context_json"])
        missing = _missing(context, row["mode"])
        if missing:
            return {"response": {"task_id": task_id, "status": "needs_context", "missing": missing}}
        kind = "reconstructed" if row["mode"] == "reconstruct" else "original"
        request = {"task_id": task_id, "kind": kind, "context": context, "context_version": row["context_version"],
                   "trace_origin": kind}
        intent = fingerprint({**request, **({"requested_recipe_id": recipe_id} if recipe_id else {}),
                              **({"recall_memory": True} if recall_memory else {})})
        operation = "start_execution:" + task_id
        saved = self._store.replay(db, operation, key, intent)
        if saved is not None:
            return {"response": saved}
        return {"request": request, "intent": intent, "operation": operation, "kind": kind, "objective": context["objective"]}

    async def start_execution(self, task_id, *, key, recipe_id=None, recall_memory=False):
        key = stable_key(key)
        if type(recall_memory) is not bool:
            raise ValueError("recall_memory must be boolean")
        if recall_memory and self._atoms is None:
            raise ValueError("recall_memory requires a client")
        with self._store.transaction() as db:
            first = self._execution_intent(db, task_id, key, recipe_id, recall_memory)
        if "response" in first:
            return result(first["response"])
        # Recall runs outside the transaction, so a concurrent caller with this key is re-checked below.
        memory = await self._recall_snapshot(first["objective"]) if recall_memory else None
        with self._store.transaction() as db:
            intent = self._execution_intent(db, task_id, key, recipe_id, recall_memory)
            if "response" in intent:
                return result(intent["response"])
            if intent["intent"] != first["intent"]:
                raise ValueError("task context changed while recalling Memory; start the execution again")
            request = intent["request"]
            head = self._head(db, task_id)
            if recipe_id is not None:
                method = self._method(db, recipe_id)
                if head and head["recipe_id"] != recipe_id:
                    raise ValueError("task is already bound to another Recipe")
                if not head:
                    db.execute("INSERT INTO task_recipes VALUES(?,?)", (task_id, recipe_id))
                request["method"] = method
            elif head and head["current_candidate_id"]:
                request["method"] = self._method(db, head["recipe_id"])
            if memory is not None:
                request["memory"] = memory
            request_sha, execution_id = fingerprint(request), "execution_" + uuid4().hex
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,NULL)",
                       (execution_id, task_id, intent["kind"], "awaiting_execution", canonical_json(request), request_sha))
            response = {"task_id": task_id, "execution_id": execution_id, "status": "awaiting_execution",
                        "request_sha256": request_sha, "request": request}
            self._store.remember(db, intent["operation"], key, intent["intent"], response)
        return result(response)

    async def get_execution(self, execution_id):
        row = self._store.read_one("SELECT * FROM executions WHERE id=?", (execution_id,))
        if row is None:
            raise LookupError("execution not found")
        saved = json.loads(row["result_json"]) if row["result_json"] else None
        return result({"execution_id": execution_id, "task_id": row["task_id"], "kind": row["kind"], "status": row["status"],
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
            refs = self._artifact_refs(db, artifacts)
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
                "SELECT r.*,e.request_json FROM revisions r JOIN executions e ON e.id=r.execution_id WHERE r.id=?",
                (revision_id,)).fetchone()
            if row is None:
                raise LookupError("revision not found")
            # Resolve replay before reading mutable feedback or the current Recipe.
            old = db.execute("SELECT response_json FROM idempotency WHERE operation=? AND request_key=?", (operation, key)).fetchone()
            if old is not None:
                return result(json.loads(old["response_json"]))
            previous = json.loads(row["request_json"])
            request = {"task_id": row["task_id"], "kind": "revision", "trace_origin": "revision", "context": previous["context"],
                       "context_version": previous["context_version"], "base_revision_id": revision_id,
                       "base_result": json.loads(row["result_json"]), "feedback": self._revision_feedback(db, revision_id)}
            head = self._head(db, row["task_id"])
            if head and head["current_candidate_id"]:
                request["method"] = self._method(db, head["recipe_id"])
            digest, execution_id = fingerprint(request), "execution_" + uuid4().hex
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,NULL)",
                       (execution_id, row["task_id"], "revision", "awaiting_execution", canonical_json(request), digest))
            response = {"task_id": row["task_id"], "execution_id": execution_id, "status": "awaiting_execution",
                        "request_sha256": digest, "request": request}
            self._store.remember(db, operation, key, digest, response)
        return result(response)
