from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from cosens_rsi.algorithms.hierarchical import seed_bundle, freeze_bundle, fingerprint
from cosens_rsi.experience_runtime import ExperienceRuntime
from cosens_rsi.observability import CaseObservability
from cosens_rsi.improvement import HarnessImprovement
from cosens_rsi.recipe_observability import RecipeObservability
from cosens_rsi.task_packages import TaskPackages
from cosens_rsi.generic_improvement import GenericImprovement
from cosens_rsi.recipe_memory import RecipeMemory
from cosens_rsi.lifecycle_observability import LifecycleObservability

logger = logging.getLogger(__name__)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value) -> str:
    raw = value if isinstance(value, bytes) else canonical(value).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryDependencyError(RuntimeError):
    def __init__(self, status_code=None):
        super().__init__("Friday Memory dependency unavailable")
        self.status_code = status_code


class RSIService(ExperienceRuntime, CaseObservability, HarnessImprovement, RecipeObservability,
                 TaskPackages, GenericImprovement, RecipeMemory, LifecycleObservability):
    def __init__(self, db_path: str | None = None, *, memory_url: str | None = None,
                 memory_token: str | None = None, memory_project: str | None = None):
        self.db_path = db_path or os.environ.get("COSENS_RSI_DB", "./var/cosens-rsi.sqlite3")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.memory_url = (memory_url or os.environ.get("COSENS_MEMORY_URL", "http://127.0.0.1:18104/memory")).rstrip("/")
        self.memory_token = memory_token or os.environ.get("COSENS_MEMORY_INTERNAL_TOKEN", "")
        self.memory_project = memory_project or os.environ.get("COSENS_MEMORY_PROJECT", "default")
        self.token = os.environ.get("COSENS_RSI_TOKEN", "local-rsi-token")
        self.admin_token = os.environ.get("COSENS_RSI_ADMIN_TOKEN", "local-rsi-admin-token")
        self.runtime_mode = os.environ.get("COSENS_RSI_RUNTIME_TRIGGER", "manual")
        self.improvement_mode = os.environ.get("COSENS_RSI_IMPROVEMENT_TRIGGER", "manual")
        self.auto_patterns = [re.compile(x, re.I) for x in os.environ.get(
            "COSENS_RSI_AUTO_PATTERNS", r"(multi.step|repeatable workflow|按流程|按步骤|标准流程)"
        ).split(";;") if x]
        prompt_path = Path(os.environ.get("COSENS_RSI_TRIGGER_PROMPT", "config/trigger-classifier-prompt.md"))
        self.trigger_prompt = prompt_path.read_text() if prompt_path.exists() else (
            "Select RSI only for a concrete task that benefits from a repeatable hierarchical workflow."
        )
        self.trigger_prompt_hash = sha(self.trigger_prompt)
        self._init_db()

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS recipes(
              id TEXT PRIMARY KEY, family TEXT NOT NULL, revision INTEGER NOT NULL,
              bundle_json TEXT NOT NULL, bundle_hash TEXT NOT NULL, source TEXT NOT NULL,
              created_at TEXT NOT NULL, UNIQUE(family,revision));
            CREATE TABLE IF NOT EXISTS active_recipes(
              family TEXT PRIMARY KEY, recipe_id TEXT NOT NULL, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY, principal TEXT NOT NULL, project TEXT NOT NULL,
              task_json TEXT NOT NULL, task_hash TEXT NOT NULL, family TEXT NOT NULL,
              status TEXT NOT NULL, execution_mode TEXT NOT NULL, recipe_id TEXT,
              recipe_revision INTEGER, snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
              checkpoint_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
              body_json TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS feedback(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body_json TEXT NOT NULL,
              created_at TEXT NOT NULL, FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE TABLE IF NOT EXISTS approvals(
              id TEXT PRIMARY KEY, principal TEXT NOT NULL, task_json TEXT NOT NULL,
              status TEXT NOT NULL, expires REAL NOT NULL, created_at TEXT NOT NULL, run_id TEXT);
            CREATE TABLE IF NOT EXISTS idempotency_records(
              principal TEXT NOT NULL, operation TEXT NOT NULL, idempotency_key TEXT NOT NULL,
              request_hash TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT,
              created_at REAL NOT NULL, PRIMARY KEY(principal,operation,idempotency_key));
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, resource_id TEXT,
              state TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(
              id TEXT PRIMARY KEY, principal TEXT NOT NULL, name TEXT NOT NULL,
              content TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS step_attempts(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
              attempt_no INTEGER NOT NULL, token_hash TEXT NOT NULL, status TEXT NOT NULL,
              error_json TEXT, created_at TEXT NOT NULL,
              UNIQUE(run_id,node_id,attempt_no), FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE INDEX IF NOT EXISTS runs_project ON runs(project,created_at);
            CREATE INDEX IF NOT EXISTS event_page ON events(run_id,seq);
            """)
        self.init_experience_schema()
        self.init_recipe_memory_schema()
        self.init_observability_schema()
        self.init_task_package_schema()
        self.init_improvement_schema()

    def _memory(self, method: str, path: str, body: dict | None = None):
        if not self.memory_token:
            logger.error("Friday Memory internal credential is not configured")
            raise RuntimeError("Friday Memory internal credential is not configured")
        try:
            response = httpx.request(method, self.memory_url + path, json=body,
                headers={"Authorization": f"Bearer {self.memory_token}"}, timeout=12)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.exception("Friday Memory dependency request failed (%s %s)", method, path)
            status_code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            raise MemoryDependencyError(status_code) from exc

    def health(self):
        memory = self._memory("GET", "/internal/v1/health")
        return {"status": "ok", "storage": "sqlite-local", "memory": memory,
                "trigger_policy": {"runtime": self.runtime_mode, "improvement": self.improvement_mode},
                "trigger_classifier": "configured_prompt_classifier" if os.environ.get("COSENS_RSI_TRIGGER_CLASSIFIER_URL") else "keyword_dev_fallback",
                "memory_outbox": self.outbox_status(),
                "harness_improvement": {"profile": "local-isolated-evaluation-v1",
                                        "configured_families": sorted(self.improvement_backends),
                                        "generic_preparation": True,
                                        "generic_runtime": self.generic_runtime.version if self.generic_runtime else None},
                "capabilities": ["assist", "guided", "memory-wiki", "memory-recall", "experience-reranking",
                                 "versioned-experience-writeback", "feedback", "artifact", "mcp"]}

    def _event(self, db, run_id: str, kind: str, body: dict):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, seq, kind, canonical(body), now()))

    def get_active_recipe(self, family: str):
        with self._db() as db:
            row = db.execute("""SELECT r.* FROM active_recipes a JOIN recipes r ON r.id=a.recipe_id
                WHERE a.family=?""", (family,)).fetchone()
        if not row:
            return {"status": "not_found", "task_family": family}
        return {"status": "matched", "recipe_id": row["id"], "revision": row["revision"],
                "bundle_hash": row["bundle_hash"], "bundle": json.loads(row["bundle_json"])}

    def bootstrap_seed(self, family: str, reason: str):
        if not reason.strip():
            raise ValueError("reason required")
        bundle = seed_bundle()
        frozen = freeze_bundle(bundle)
        rid = "recipe_" + uuid4().hex
        with self._db() as db:
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM recipes WHERE family=?", (family,)).fetchone()[0]
            db.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)",
                (rid, family, revision, canonical(frozen["bundle"]), "sha256:" + frozen["sha256"], "manual_seed:" + reason, now()))
            db.execute("INSERT INTO active_recipes VALUES(?,?,?) ON CONFLICT(family) DO UPDATE SET recipe_id=excluded.recipe_id,revision=excluded.revision",
                (family, rid, revision))
        return {"recipe_id": rid, "revision": revision, "bundle_hash": "sha256:" + frozen["sha256"],
                "status": "active_seed_unvalidated"}

    def bootstrap_recipe_bundle(self, family: str, bundle: dict, reason: str, *, admin_token: str):
        """Import an initial custom Recipe, never replace an existing family."""
        if admin_token != self.admin_token:
            raise PermissionError("admin credential required")
        if not isinstance(family, str) or not family.strip() or len(family) > 160 or not reason.strip():
            raise ValueError("task family and bootstrap reason required")
        frozen = freeze_bundle(bundle)
        rid = "recipe_" + uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM recipes WHERE family=?", (family,)).fetchone():
                raise ValueError("family already initialized; use validated improvement release")
            db.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)", (rid, family, 1,
                canonical(frozen["bundle"]), "sha256:" + frozen["sha256"], "admin_bootstrap_unvalidated:" + reason, now()))
            db.execute("INSERT INTO active_recipes VALUES(?,?,?)", (family, rid, 1))
        return {"recipe_id": rid, "revision": 1, "bundle_hash": "sha256:" + frozen["sha256"],
                "status": "active_seed_unvalidated"}

    def decide_trigger(self, task: dict, *, explicit: bool = False, mode: str | None = None):
        mode = mode or self.runtime_mode
        if mode not in {"auto", "approval", "manual"}:
            raise ValueError("invalid trigger mode")
        text = str(task.get("objective", ""))
        classifier = "keyword_dev"
        reason = "no_prompt_match"
        classifier_url = os.environ.get("COSENS_RSI_TRIGGER_CLASSIFIER_URL")
        if mode == "auto" and classifier_url:
            classifier = "configured_prompt_classifier"
            headers = {}
            api_key = os.environ.get("COSENS_RSI_TRIGGER_CLASSIFIER_KEY")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            try:
                response = httpx.post(classifier_url, json={"system_prompt": self.trigger_prompt,
                    "task": task, "response_schema": {"should_trigger": "boolean", "reason": "string"}},
                    headers=headers, timeout=8)
                response.raise_for_status()
                decision = response.json()
                if type(decision.get("should_trigger")) is not bool:
                    raise ValueError("classifier returned invalid schema")
                match = decision["should_trigger"]
                reason = str(decision.get("reason", "configured_prompt"))[:300]
            except Exception as exc:
                raise RuntimeError("configured RSI trigger classifier unavailable or returned invalid output") from exc
        else:
            match = any(pattern.search(text) for pattern in self.auto_patterns)
            if match:
                reason = "configured_keyword_pattern"
        if mode == "manual":
            return {"decision": "execute" if explicit else "do_not_trigger", "reason": "explicit_invocation" if explicit else "manual_mode",
                    "prompt_version": self.trigger_prompt_hash}
        if mode == "auto":
            return {"decision": "execute" if match or explicit else "do_not_trigger",
                    "reason": reason if match else "explicit_invocation" if explicit else "no_prompt_match",
                    "classifier": classifier, "prompt_version": self.trigger_prompt_hash}
        return {"decision": "approval_required" if (match or explicit) else "do_not_trigger",
                "reason": "prompt_match" if match else "explicit_invocation" if explicit else "no_prompt_match",
                "prompt_version": self.trigger_prompt_hash}

    def request_approval(self, task: dict, principal: str, *, explicit: bool = False):
        decision = self.decide_trigger(task, mode="approval", explicit=explicit)
        if decision["decision"] != "approval_required":
            return decision
        aid = "approval_" + uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,NULL)",
                (aid, principal, canonical(task), "pending", time.time()+900, now()))
        return {"decision": "approval_required", "approval_id": aid, "expires_in_seconds": 900,
                "scope_hash": sha(task), "reason": decision["reason"]}

    def approve(self, approval_id: str, *, admin_token: str):
        if admin_token != self.admin_token:
            raise PermissionError("admin approval channel required")
        with self._db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise LookupError("approval not found")
            if row["status"] != "pending" or row["expires"] <= time.time():
                raise ValueError("approval expired or already consumed")
            task = json.loads(row["task_json"])
            run = self._prepare(task, row["principal"], explicit=True)
            db.execute("UPDATE approvals SET status='approved',run_id=? WHERE id=? AND status='pending'", (run["run_id"], approval_id))
        return run

    def prepare_task(self, task: dict, principal: str, *, explicit: bool = True, request_key: str | None = None):
        op = "prepare_task"
        request_hash = sha({"task": task, "explicit": explicit, "trigger_mode": self.runtime_mode})
        if request_key:
            with self._db() as db:
                row = db.execute("SELECT * FROM idempotency_records WHERE principal=? AND operation=? AND idempotency_key=?",
                    (principal, op, request_key)).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise ValueError("idempotency key reused with different request")
                    if row["state"] == "done":
                        return json.loads(row["response_json"])
                    if row["created_at"] > time.time() - 300:
                        return {"status": "processing", "retry_after_seconds": 2}
                    db.execute("DELETE FROM idempotency_records WHERE principal=? AND operation=? AND idempotency_key=?",
                        (principal, op, request_key))
                db.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)",
                    (principal, op, request_key, request_hash, "processing", None, time.time()))
        try:
            decision = self.decide_trigger(task, explicit=explicit)
            if decision["decision"] == "do_not_trigger":
                result = {"status": "not_triggered", **decision}
            elif self.runtime_mode == "approval":
                result = self.request_approval(task, principal, explicit=explicit)
            else:
                result = self._prepare(task, principal, explicit=explicit)
            if request_key:
                with self._db() as db:
                    db.execute("UPDATE idempotency_records SET state='done',response_json=? WHERE principal=? AND operation=? AND idempotency_key=?",
                        (canonical(result), principal, op, request_key))
            return result
        except Exception:
            if request_key:
                with self._db() as db:
                    db.execute("DELETE FROM idempotency_records WHERE principal=? AND operation=? AND idempotency_key=? AND state='processing'",
                        (principal, op, request_key))
            raise

    def _prepare(self, task: dict, principal: str, *, explicit: bool):
        objective = str(task.get("objective", "")).strip()
        family = str(task.get("task_family", "general")).strip() or "general"
        if not objective or len(objective) > 20000:
            raise ValueError("task objective required and limited to 20000 characters")
        task_package = {"objective": objective, "task_family": family,
            "input_artifact_ids": task.get("input_artifact_ids", []),
            "deliverable": task.get("deliverable", {}), "acceptance": task.get("acceptance", {}),
            "environment": task.get("environment", {})}
        lineage_id = task.get("task_lineage_id")
        if lineage_id is not None:
            if not isinstance(lineage_id, str) or not lineage_id.strip() or len(lineage_id) > 256:
                raise ValueError("task_lineage_id must be non-empty text up to 256 characters")
            task_package["task_lineage_id"] = lineage_id
        input_ids = task_package["input_artifact_ids"]
        if (not isinstance(input_ids, list) or any(not isinstance(a, str) for a in input_ids)
                or len(input_ids) > 100 or len(set(input_ids)) != len(input_ids)):
            raise ValueError("initial inputs require unique artifact IDs")
        with self._db() as db:
            if any(not db.execute("SELECT 1 FROM artifacts WHERE id=? AND principal=?", (a, principal)).fetchone()
                   for a in input_ids):
                raise LookupError("initial input unavailable")
        mode = task.get("execution_mode", "assist")
        if mode not in {"assist", "guided", "managed"}:
            raise ValueError("execution_mode must be assist, guided, or managed")
        if mode == "managed":
            raise RuntimeError("managed runner is not configured; choose assist or guided")
        learning = self.prepare_learning_context(task)
        recipe = self.get_active_recipe(family)
        learning["recipe_compatibility"] = recipe.get("bundle_hash")
        wiki = self._memory("POST", "/internal/v1/wiki/search", {"query": objective, "limit": 8})
        memory_items = wiki.get("items", [])
        expansion = []
        if len(memory_items) < 2 or bool(task.get("requires_evidence")):
            recall = self._memory("POST", "/internal/v1/recall", {"query": objective, "token_budget": 1800})
            expansion = recall.get("items", [])
        snapshot = {"schema_version": 1, "profile": wiki.get("profile"), "query": objective,
            "created_at": now(), "wiki": memory_items, "evidence": expansion,
            "coverage": wiki.get("coverage"), "sufficiency": "unknown" if not memory_items and not expansion else "partial",
            "experience_context": learning}
        procedural = self.recall_recipe_memory(principal, family, objective, recipe.get('bundle_hash'), learning['watermark'])
        if procedural is not None:
            snapshot['recipe_memory'] = procedural
        snapshot_hash = sha(snapshot)
        rid = "run_" + uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, principal, self.memory_project, canonical(task_package), sha(task_package), family,
                 "ready", mode, recipe.get("recipe_id"), recipe.get("revision"), canonical(snapshot), snapshot_hash,
                 "{}", None, now(), now()))
            db.execute("INSERT INTO run_learning_context VALUES(?,?)", (rid, canonical(learning)))
            self._event(db, rid, "prepared", {"recipe_status": recipe["status"], "snapshot_hash": snapshot_hash,
                "memory_items": len(memory_items), "expanded_items": len(expansion), "explicit": explicit})
            # The saved task/snapshot and immutable input IDs are canonical
            # initial evidence, independent from the dashboard trace importer.
            initial_event = {"id": "prepared", "kind": "user_prompt", "payload": {
                "task": task_package, "snapshot_hash": snapshot_hash, "input_artifact_ids": input_ids}}
            db.execute("INSERT INTO canonical_session_events VALUES(?,?,?,?,?)",
                (rid, "prepared", fingerprint(initial_event), canonical(initial_event), time.time()))
            self._wake_generic(db, principal)
        return {"run_id": rid, "status": "ready", "task_package": task_package,
            "task_package_hash": sha(task_package), "execution_mode": mode,
            "recipe": recipe, "context": {"snapshot_id": "snapshot_" + snapshot_hash[7:23],
                "hash": snapshot_hash, "sufficiency": snapshot["sufficiency"], "wiki": memory_items,
                "evidence": expansion, **({'recipe_memory': procedural} if procedural is not None else {})}}

    def get_run(self, run_id: str, principal: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND principal=?", (run_id, principal)).fetchone()
        if not row:
            raise LookupError("run not found")
        from .evidence import validate_snapshot
        validate_snapshot(self, json.loads(row["snapshot_json"]))
        return {"run_id": row["id"], "status": row["status"], "execution_mode": row["execution_mode"],
            "task_package": json.loads(row["task_json"]), "task_package_hash": row["task_hash"],
            "recipe_id": row["recipe_id"], "recipe_revision": row["recipe_revision"],
            "snapshot": json.loads(row["snapshot_json"]), "snapshot_hash": row["snapshot_hash"],
            "checkpoint": json.loads(row["checkpoint_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def list_runs(self, principal: str, *, limit: int = 100):
        limit = max(1, min(int(limit), 200))
        with self._db() as db:
            rows = db.execute("""SELECT id,task_json,family,status,execution_mode,recipe_id,
                recipe_revision,created_at,updated_at FROM runs WHERE principal=?
                ORDER BY created_at DESC LIMIT ?""", (principal, limit)).fetchall()
        return [{"run_id": row["id"], "objective": json.loads(row["task_json"]).get("objective", ""),
            "task_family": row["family"], "status": row["status"], "execution_mode": row["execution_mode"],
            "recipe_id": row["recipe_id"], "recipe_revision": row["recipe_revision"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]} for row in rows]

    def get_run_trace(self, run_id: str, principal: str):
        run = self.get_run(run_id, principal)
        learning = self._learning_context(run_id)
        with self._db() as db:
            decisions = db.execute("SELECT id FROM decisions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return {"run": run, "events": self.get_events(run_id, principal),
            "learning": learning, "decisions": [self.get_decision(run_id, principal, row["id"]) for row in decisions]}

    def guidance(self, run_id: str, principal: str, observation: str = "", candidates=None,
                 node_id=None, request_key=None):
        if candidates is not None:
            return self.decision_guidance(run_id, principal, observation, candidates, node_id, request_key)
        run = self.get_run(run_id, principal)
        context = run["snapshot"]
        return {"run_id": run_id, "execution_mode": run["execution_mode"],
            "recipe_id": run["recipe_id"], "memory_snapshot_hash": run["snapshot_hash"],
            "guidance": "Use the frozen task and evidence. Verify claims against cited source material; ask for missing information when needed.",
            "observation_received": bool(observation.strip()), "memory_context": context,
            "next_action": "continue_task", "decision_available": True,
            "decision_protocol": "Provide candidates and observation to obtain an experience-ranked recommendation; submit the actual action through checkpoint (assist) or submit_step (guided)."}

    def checkpoint(self, run_id: str, principal: str, checkpoint: dict):
        if len(canonical(checkpoint)) > 16000:
            raise ValueError("checkpoint too large")
        run = self.get_run(run_id, principal)
        if any(k in checkpoint for k in ("guided_index", "guided_outputs")):
            raise ValueError("guided progress is owned by the step protocol")
        if ("decision_id" in checkpoint) != ("actual_action_id" in checkpoint):
            raise ValueError("decision_id and actual_action_id must be supplied together")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT checkpoint_json FROM runs WHERE id=? AND principal=? AND status='ready'",
                                 (run_id, principal)).fetchone()
            if not current:
                raise LookupError("run unavailable")
            saved = {**checkpoint, **{k: v for k, v in json.loads(current[0]).items()
                                      if k in {"guided_index", "guided_outputs"}}}
            cur = db.execute("UPDATE runs SET checkpoint_json=?,updated_at=? WHERE id=? AND principal=? AND status='ready'",
                (canonical(saved), now(), run_id, principal))
            if not cur.rowcount:
                raise LookupError("run unavailable")
            if "decision_id" in checkpoint:
                if run["execution_mode"] != "assist":
                    raise ValueError("record guided actions through submit_step")
                self._record_actual(db, run, checkpoint["decision_id"], checkpoint["actual_action_id"],
                                    checkpoint.get("next_state", {}))
            self._event(db, run_id, "checkpoint", {"checkpoint_hash": sha(checkpoint)})
        return {"run_id": run_id, "status": "saved", "checkpoint_hash": sha(checkpoint)}

    def submit_result(self, run_id: str, principal: str, result: dict):
        if self.get_run(run_id, principal)["execution_mode"] != "assist":
            raise ValueError("guided runs must complete through their step protocol")
        if len(canonical(result)) > 64000:
            raise ValueError("result too large")
        with self._db() as db:
            cur = db.execute("UPDATE runs SET status='completed',result_json=?,updated_at=? WHERE id=? AND principal=? AND status='ready'",
                (canonical(result), now(), run_id, principal))
            if not cur.rowcount:
                raise LookupError("run unavailable or already completed")
            self._event(db, run_id, "submitted", {"result_hash": sha(result), "evaluation": "inconclusive"})
        return {"run_id": run_id, "status": "completed", "evaluation": "inconclusive", "accepted": None}

    def record_feedback(self, run_id: str, principal: str, feedback: dict):
        self._authorize_evidence_write(run_id, principal)
        if not isinstance(feedback, dict) or len(canonical(feedback).encode()) > 64000:
            raise ValueError("feedback must be an object up to 64 KB")
        fid = "feedback_" + uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM feedback WHERE run_id=? AND body_json=? ORDER BY created_at LIMIT 1",
                                  (run_id, canonical(feedback))).fetchone()
            if existing:
                fid = existing[0]
            else:
                db.execute("INSERT INTO feedback VALUES(?,?,?,?)", (fid, run_id, canonical(feedback), now()))
                self._event(db, run_id, "feedback", {"feedback_id": fid, "type": feedback.get("type", "unspecified")})
                event = {"id": fid, "kind": "correction", "payload": {"feedback_id": fid, "body": feedback,
                         "author": principal, "authority": "run_owner"}}
                db.execute("INSERT INTO canonical_session_events VALUES(?,?,?,?,?)",
                           (run_id, fid, fingerprint(event), canonical(event), time.time()))
                self._wake_generic(db, principal)
        response = {"feedback_id": fid, "run_id": run_id, "status": "recorded", "shared_memory_write": False}
        if self.improvement_mode == "auto" and (feedback.get("type") in {"correction", "expert_correction"}
                                                or feedback.get("acceptance_checks")):
            # Identical correction retries reuse their audit record and cannot
            # launch duplicate learning jobs. Manual/approval policy is kept.
            correction_hash = fingerprint({"run_id": run_id, "feedback": feedback})
            response["improvement"] = self.start_improvement(run_id, principal,
                "Learn from an attributable correction", request_key="correction:" + correction_hash)
        return response

    def store_artifact(self, principal: str, name: str, content: str):
        if not name.strip() or not content or len(content.encode()) > 1_000_000:
            raise ValueError("artifact name/content required; content limit is 1 MB")
        aid = "artifact_" + uuid4().hex
        content_hash = sha(content)
        with self._db() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                (aid, principal, name[:256], content, content_hash, now()))
        return {"artifact_id": aid, "name": name[:256], "content_hash": content_hash,
                "size_bytes": len(content.encode())}

    def next_step(self, run_id: str, principal: str):
        run = self.get_run(run_id, principal)
        if run["execution_mode"] != "guided":
            raise ValueError("step protocol requires execution_mode=guided")
        if run["status"] != "ready":
            raise ValueError("run is not active")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] != "ready":
                raise ValueError("run is no longer active")
            checkpoint = json.loads(db.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            recipe = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (run["recipe_id"],)).fetchone()
            if not recipe:
                raise LookupError("frozen recipe missing")
            bundle = json.loads(recipe["bundle_json"])
            nodes = bundle["flow"]["nodes"]
            index = int(checkpoint.get("guided_index", 0))
            if index >= len(nodes):
                return {"run_id": run_id, "status": "all_steps_passed", "next_action": "complete_run"}
            node = nodes[index]
            previous = db.execute("SELECT id,status,error_json FROM step_attempts WHERE run_id=? AND node_id=? ORDER BY attempt_no DESC LIMIT 1",
                (run_id, node["id"])).fetchone()
            if previous and previous["status"] == "pending":
                raise ValueError("step attempt already issued; submit its attempt token")
            attempt_no = db.execute("SELECT COUNT(*) FROM step_attempts WHERE run_id=? AND node_id=?",
                (run_id, node["id"])).fetchone()[0] + 1
            max_attempts = int(node.get("max_repairs", 0)) + 1
            if attempt_no > max_attempts:
                db.execute("UPDATE runs SET status='failed',updated_at=? WHERE id=?", (now(), run_id))
                self._event(db, run_id, "step_failed", {"node_id": node["id"], "reason": "repair limit exhausted"})
                return {"run_id": run_id, "status": "failed", "node_id": node["id"], "reason": "repair limit exhausted"}
            outputs = checkpoint.get("guided_outputs", {})
            bound = {}
            for port, source in node["inputs"].items():
                if source == "$task":
                    bound[port] = run["task_package"]["objective"]
                else:
                    source_node, source_port = source.split(".", 1)
                    if source_port not in outputs.get(source_node, {}):
                        raise ValueError("required predecessor output is missing")
                    bound[port] = outputs[source_node][source_port]
            skill_ref = bundle["bindings"][node["skill"]]
            step = bundle["steps"][skill_ref]
            token = uuid4().hex + uuid4().hex
            attempt_id = "attempt_" + uuid4().hex
            error = json.loads(previous["error_json"]) if previous and previous["error_json"] else []
            db.execute("INSERT INTO step_attempts VALUES(?,?,?,?,?,?,?,?)",
                (attempt_id, run_id, node["id"], attempt_no, sha(token), "pending", None, now()))
            self._event(db, run_id, "step_issued", {"node_id": node["id"], "attempt_id": attempt_id, "attempt": attempt_no})
        return {"run_id": run_id, "status": "step_ready", "node": {"id": node["id"], "goal": node["goal"],
            "recipe_ref": skill_ref, "method": step["body"], "inputs": bound,
            "output_contract": step["outputs"], "flow_checks": bundle["flow"]["checks"]},
            "attempt_id": attempt_id, "attempt_token": token, "attempt": attempt_no,
            "repair_errors": error, "max_repairs": int(node.get("max_repairs", 0)),
            "memory_context": run['snapshot']}

    def submit_step(self, run_id: str, principal: str, attempt_id: str, attempt_token: str, outputs: dict,
                    decision_id=None, actual_action_id=None):
        run = self.get_run(run_id, principal)
        if run["status"] != "ready" or run["execution_mode"] != "guided":
            raise ValueError("step submission requires an active guided run")
        if (decision_id is None) != (actual_action_id is None):
            raise ValueError("decision_id and actual_action_id must be supplied together")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] != "ready":
                raise ValueError("run is no longer active")
            attempt = db.execute("SELECT * FROM step_attempts WHERE id=? AND run_id=?", (attempt_id, run_id)).fetchone()
            if not attempt or attempt["status"] != "pending" or attempt["token_hash"] != sha(attempt_token):
                raise PermissionError("invalid or consumed attempt token")
            bundle_row = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (run["recipe_id"],)).fetchone()
            bundle = json.loads(bundle_row[0])
            node = next(node for node in bundle["flow"]["nodes"] if node["id"] == attempt["node_id"])
            ref = bundle["bindings"][node["skill"]]
            contract = bundle["steps"][ref]["outputs"]
            errors = []
            if set(outputs) != set(contract):
                errors.append("output ports do not match frozen contract")
            normalized = {}
            for port, kind in contract.items():
                value = outputs.get(port)
                if kind == "text":
                    if not isinstance(value, str) or not value.strip() or len(value) > 100_000:
                        errors.append(f"{port} must be nonempty text up to 100000 characters")
                    else:
                        normalized[port] = value
                elif kind == "artifacts":
                    if not isinstance(value, list) or not value:
                        errors.append(f"{port} must contain at least one uploaded artifact")
                        continue
                    refs = []
                    for item in value:
                        if not isinstance(item, dict):
                            errors.append(f"{port} has an invalid artifact reference")
                            continue
                        artifact = db.execute("SELECT id,name,content_hash FROM artifacts WHERE id=? AND principal=?",
                            (item.get("artifact_id"), principal)).fetchone()
                        if not artifact or artifact["content_hash"] != item.get("content_hash"):
                            errors.append(f"{port} has an unavailable artifact reference")
                        else:
                            refs.append({"artifact_id": artifact["id"], "name": artifact["name"],
                                         "content_hash": artifact["content_hash"]})
                    if refs:
                        normalized[port] = refs
                else:
                    errors.append(f"unsupported output type {kind}")
            if errors:
                if decision_id:
                    self._record_actual(db, run, decision_id, actual_action_id,
                        {"contract_status": "rejected", "errors": errors}, node_id=attempt["node_id"])
                db.execute("UPDATE step_attempts SET status='rejected',error_json=? WHERE id=?",
                    (canonical(errors), attempt_id))
                self._event(db, run_id, "step_rejected", {"node_id": attempt["node_id"], "errors": errors})
                return {"run_id": run_id, "status": "repair_required", "errors": errors}
            checkpoint = json.loads(db.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            saved = checkpoint.setdefault("guided_outputs", {})
            saved[attempt["node_id"]] = normalized
            if decision_id:
                self._record_actual(db, run, decision_id, actual_action_id,
                    {"contract_status": "passed", "outputs_hash": sha(normalized)}, node_id=attempt["node_id"])
            checkpoint["guided_index"] = int(checkpoint.get("guided_index", 0)) + 1
            db.execute("UPDATE runs SET checkpoint_json=?,updated_at=? WHERE id=?",
                (canonical(checkpoint), now(), run_id))
            db.execute("UPDATE step_attempts SET status='passed' WHERE id=?", (attempt_id,))
            self._event(db, run_id, "step_passed", {"node_id": attempt["node_id"], "outputs": normalized})
        return {"run_id": run_id, "status": "step_passed", "node_id": attempt["node_id"], "outputs": normalized}

    def complete_guided_run(self, run_id: str, principal: str):
        run = self.get_run(run_id, principal)
        if run["execution_mode"] != "guided":
            raise ValueError("complete_guided_run requires guided mode")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0] != "ready":
                raise ValueError("run is no longer active")
            recipe_row = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (run["recipe_id"],)).fetchone()
            bundle = json.loads(recipe_row[0])
            checkpoint = json.loads(db.execute("SELECT checkpoint_json FROM runs WHERE id=?", (run_id,)).fetchone()[0])
            if int(checkpoint.get("guided_index", 0)) != len(bundle["flow"]["nodes"]):
                raise ValueError("guided workflow has unfinished nodes")
            node_id, port = bundle["flow"]["final"].split(".", 1)
            final = checkpoint["guided_outputs"][node_id][port]
            result = {"final": final, "bundle_hash": sha(bundle), "snapshot_hash": run["snapshot_hash"]}
            db.execute("UPDATE runs SET status='completed',result_json=?,updated_at=? WHERE id=?",
                (canonical(result), now(), run_id))
            self._event(db, run_id, "guided_completed", {"result_hash": sha(result)})
        return {"run_id": run_id, "status": "completed", "result": result, "evaluation": "inconclusive"}

    def get_events(self, run_id: str, principal: str):
        self.get_run(run_id, principal)
        with self._db() as db:
            rows = db.execute("SELECT seq,kind,body_json,created_at FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [{"sequence": r["seq"], "type": r["kind"], "data": json.loads(r["body_json"]), "created_at": r["created_at"]} for r in rows]

