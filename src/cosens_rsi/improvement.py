"""Local harness jobs and explicit, revision-checked Recipe publication.

The experiment registry is private to a job. Its promotions never change the
production active pointer. Server configuration owns every executable adapter.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4

from .algorithms.hierarchical import fingerprint, freeze_bundle
from .algorithms.hierarchical_learning import BundleRegistry, HierarchicalLearner, compare, confirm_decision

logger = logging.getLogger(__name__)


class HarnessImprovement:
    def init_improvement_schema(self):
        from .runtime import load_runtime
        self.generic_runtime = load_runtime()
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS harness_jobs(
              job_id TEXT PRIMARY KEY REFERENCES jobs(id), principal TEXT NOT NULL,
              base_recipe_id TEXT NOT NULL, report_json TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS recipe_releases(
              id TEXT PRIMARY KEY, family TEXT NOT NULL, job_id TEXT NOT NULL,
              previous_recipe_id TEXT NOT NULL, recipe_id TEXT NOT NULL,
              reason TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
        self.improvement_backends = {}
        factory_path = os.environ.get("COSENS_RSI_HARNESS_BACKEND")
        if factory_path:
            module, separator, name = factory_path.partition(":")
            if not separator:
                raise ValueError("COSENS_RSI_HARNESS_BACKEND must be module:factory")
            backends = getattr(importlib.import_module(module), name)()
            if not isinstance(backends, dict) or any(not isinstance(k, str) or not hasattr(v, "open_session")
                                                     for k, v in backends.items()):
                raise ValueError("harness backend factory must return task-family adapters")
            self.improvement_backends = backends
        if self.generic_runtime is not None and self.generic_runtime.isolated:
            with self._db() as db:
                db.execute("UPDATE jobs SET state='queued' WHERE state='awaiting_runner' "
                           "AND id IN (SELECT job_id FROM generic_improvement_jobs)")

    def start_improvement(self, run_id: str, principal: str, reason: str, request_key: str | None = None):
        run = self.get_run(run_id, principal)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValueError("improvement reason required (up to 4000 characters)")
        request_hash = fingerprint({"run_id": run_id, "reason": reason})
        request_key = request_key or "harness:" + request_hash
        if not isinstance(request_key, str) or not request_key or len(request_key) > 256:
            raise ValueError("improvement request_key must be text up to 256 characters")
        if self.improvement_mode == "approval":
            return {"status": "approval_required", "reason": "improvement requires authenticated human approval"}
        family = run["task_package"]["task_family"]
        backend = self.improvement_backends.get(family)
        generic = backend is None
        state = "queued" if generic or run["recipe_id"] else "deferred"
        reason_code = "source_run_has_no_recipe" if not generic and not run["recipe_id"] else None
        jid = "job_" + uuid4().hex
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT request_hash,response_json FROM idempotency_records "
                                  "WHERE principal=? AND operation='start_improvement' AND idempotency_key=?",
                                  (principal, request_key)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ValueError("idempotency key reused with different improvement request")
                return json.loads(existing[1])
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?)", (jid, "learning", run_id, state,
                json.dumps({"reason": reason, "capability": reason_code}), datetime.now(timezone.utc).isoformat()))
            if state == "queued":
                db.execute("INSERT INTO harness_jobs VALUES(?,?,?,?,?)", (jid, principal, run["recipe_id"] or "", None, None))
                if generic:
                    db.execute("INSERT INTO generic_improvement_jobs(job_id) VALUES(?)", (jid,))
            self._event(db, run_id, "improvement_requested", {"job_id": jid, "status": state})
            response = {"job_id": jid, "status": state, "reason": reason_code}
            db.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?,?,?)", (principal, "start_improvement",
                request_key, request_hash, "done", json.dumps(response), time.time()))
        return response

    def get_improvement(self, job_id: str, principal: str):
        with self._db() as db:
            row = db.execute("SELECT j.*,h.report_json,h.error FROM jobs j LEFT JOIN harness_jobs h ON h.job_id=j.id "
                             "JOIN runs r ON r.id=j.resource_id WHERE j.id=? AND r.principal=? AND j.kind='learning'",
                             (job_id, principal)).fetchone()
        if not row:
            raise LookupError("improvement job not found")
        return {"job_id": job_id, "run_id": row["resource_id"], "status": row["state"],
                "request": json.loads(row["payload_json"]),
                "report": json.loads(row["report_json"]) if row["report_json"] else None, "error": row["error"]}

    def process_one_improvement(self):
        """Single-process local worker; queued claims are atomic across sidecars.

        Running jobs are never automatically replayed after a crash because an
        external model call may have an unknown outcome.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT j.id,j.resource_id,h.principal,h.base_recipe_id FROM jobs j "
                             "JOIN harness_jobs h ON h.job_id=j.id WHERE j.state='queued' ORDER BY j.created_at LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running' WHERE id=? AND state='queued'", (row["id"],))
        jid = row["id"]
        try:
            run = self.get_run(row["resource_id"], row["principal"])
            with self._db() as db:
                generic = db.execute("SELECT 1 FROM generic_improvement_jobs WHERE job_id=?", (jid,)).fetchone()
            if generic:
                self._process_generic_improvement(row)
                return jid
            family = run["task_package"]["task_family"]
            backend = self.improvement_backends.get(family)
            if backend is None:
                raise RuntimeError("configured harness backend unavailable")
            with self._db() as db:
                recipe = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (row["base_recipe_id"],)).fetchone()
            initial = json.loads(recipe[0])
            session = backend.open_session(run)
            self.attach_recipe_memory(session, jid, row['resource_id'], row['principal'])
            root = Path(self.db_path).resolve().parent / "harness-experiments" / jid
            session.save(root)
            # Persist the frozen manifest before the first candidate run.
            report = {"schema": "harness-improvement-v1", "manifest": session.manifest,
                      "policy": asdict(session.policy), "base_recipe_id": row["base_recipe_id"],
                      "base_sha256": fingerprint(initial)}
            self._save_improvement(jid, "running", report)
            from .exposure import GlobalBundleRegistry
            registry = GlobalBundleRegistry(root, self, jid, row["principal"], session._cases)
            session.evaluation_guard = lambda partition, ids: registry.guard(session, partition, ids)
            result = HierarchicalLearner(registry, session.policy).run(family, initial, session.partitions,
                                                                      session.propose, session.evaluate)
            report["result"] = result
            candidate = result["active"]
            candidate_sha = fingerprint(candidate)
            accepted = [r for r in result["rounds"] if r["decision"]["status"] == "accepted"]
            if candidate_sha == fingerprint(initial):
                state = "deferred" if result.get("status") == "blocked" or any(
                    r["decision"]["status"] == "deferred" for r in result["rounds"]) else "no_change"
            else:
                # A chain of experimental promotions must also beat the
                # production baseline in a fresh, independent comparison.
                final = accepted[-1]["decision"]
                if len(accepted) > 1:
                    ids = [i for i in session.partitions["confirm"] if i not in registry.used_confirm(family)]
                    ids = ids[:session.policy.min_confirm_tasks]
                    if len(ids) < session.policy.min_confirm_tasks:
                        final = {"status": "deferred", "reason": "fresh production-baseline confirm tasks required"}
                    else:
                        registry.reserve_confirm(family, candidate_sha, ids)
                        comparison = compare(session.evaluate(initial, "confirm", ids), session.evaluate(candidate, "confirm", ids),
                                             ids, base_sha=fingerprint(initial), candidate_sha=candidate_sha)
                        final = confirm_decision(comparison, session.policy, candidate_sha)
                report["release_decision"] = final
                state = "validated" if final["status"] == "accepted" else "deferred"
            self._save_improvement(jid, state, report)
            with self._db() as db:
                self._event(db, run["run_id"], "improvement_finished", {"job_id": jid, "status": state})
        except Exception as exc:
            logger.exception("Harness improvement %s failed", jid)
            # Adapter output may contain secrets. Persist the exception type,
            # not raw provider errors, into user-readable job data.
            with self._db() as db:
                db.execute("UPDATE jobs SET state='failed' WHERE id=?", (jid,))
                db.execute("UPDATE harness_jobs SET error=? WHERE job_id=?", (type(exc).__name__, jid))
        return jid

    def _save_improvement(self, job_id, state, report):
        data = json.dumps(report, ensure_ascii=False, allow_nan=False)
        with self._db() as db:
            db.execute("UPDATE harness_jobs SET report_json=? WHERE job_id=?", (data, job_id))
            db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))

    def publish_improvement(self, job_id: str, expected_revision: int | None, reason: str, *, admin_token: str):
        if admin_token != self.admin_token:
            raise PermissionError("admin credential required")
        if not isinstance(reason, str) or not reason.strip() or (expected_revision is not None and type(expected_revision) is not int):
            raise ValueError("release reason and expected_revision required")
        # Validate authorized, current evidence before entering the write
        # transaction. Immutable packages and report hashes are checked again
        # inside it; unavailable Memory fails closed for publication.
        with self._db() as db:
            preparation = db.execute("SELECT g.checkpoint_json,j.state FROM generic_improvement_jobs g "
                                     "JOIN jobs j ON j.id=g.job_id WHERE g.job_id=?", (job_id,)).fetchone()
        checkpoint = json.loads(preparation[0]) if preparation else None
        if checkpoint and preparation[1] == "validated":
            for package in checkpoint.get("packages", []):
                self._validate_package(package)
            self._validate_generic_release(job_id, checkpoint)
        if self.recipe_memory_enabled:
            self.flush_recipe_memory_outbox(limit=128)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT j.state,h.*,r.family FROM jobs j JOIN harness_jobs h ON h.job_id=j.id "
                             "JOIN runs r ON r.id=j.resource_id WHERE j.id=?", (job_id,)).fetchone()
            if not row:
                raise LookupError("improvement job not found")
            if row["state"] != "validated":
                raise ValueError("publication requires independently validated improvement")
            if self.recipe_memory_enabled:
                pending=db.execute("SELECT COUNT(*) FROM recipe_memory_outbox WHERE job_id=? AND state<>'done'",(job_id,)).fetchone()[0]
                viewed=db.execute('SELECT 1 FROM recipe_learning_views WHERE job_id=? LIMIT 1',(job_id,)).fetchone()
                if pending or not viewed:raise ValueError('publication requires durable Memory evidence and training retrieval')
            active = db.execute("SELECT recipe_id,revision FROM active_recipes WHERE family=?", (row["family"],)).fetchone()
            first_release = not row["base_recipe_id"] and checkpoint is not None
            if ((first_release and (active or expected_revision is not None)) or
                    (not first_release and (not active or active[0] != row["base_recipe_id"] or active[1] != expected_revision))):
                raise ValueError("production Recipe changed; a new baseline comparison is required")
            report = json.loads(row["report_json"])
            if checkpoint:
                if report.get("checkpoint_sha256") != fingerprint(checkpoint):
                    raise ValueError("release checkpoint integrity mismatch")
                if report.get("manifest") != checkpoint.get("manifest"):
                    raise ValueError("release manifest changed")
            frozen = freeze_bundle(report["result"]["active"])
            decision = report.get("release_decision", {})
            if decision.get("status") != "accepted" or decision.get("candidate_sha256") != frozen["sha256"]:
                raise ValueError("release evidence does not bind candidate")
            rid, release = "recipe_" + uuid4().hex, "release_" + uuid4().hex
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM recipes WHERE family=?", (row["family"],)).fetchone()[0]
            created = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO recipes VALUES(?,?,?,?,?,?,?)", (rid, row["family"], revision,
                json.dumps(frozen["bundle"], ensure_ascii=False), "sha256:" + frozen["sha256"], "harness:" + job_id, created))
            if first_release:
                db.execute("INSERT INTO active_recipes VALUES(?,?,?)", (row["family"], rid, revision))
            else:
                db.execute("UPDATE active_recipes SET recipe_id=?,revision=? WHERE family=?", (rid, revision, row["family"]))
            db.execute("INSERT INTO recipe_releases VALUES(?,?,?,?,?,?,?)", (release, row["family"], job_id,
                row["base_recipe_id"], rid, reason, created))
            db.execute("UPDATE jobs SET state='published' WHERE id=?", (job_id,))
            result = {"release_id": release, "recipe_id": rid, "revision": revision, "status": "published"}
            self.record_recipe_release_memory(job_id, result, db=db)
        # New Runs freeze a different compatibility hash. Historical action
        # experience is preserved, but cannot silently cross the Recipe change.
        if self.recipe_memory_enabled:self.flush_recipe_memory_outbox()
        return result
