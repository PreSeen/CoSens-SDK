"""RSI orchestration. Long-term experience records live exclusively in Memory."""
from __future__ import annotations

import json
import math
import os
import time
from uuid import uuid4

from memory.experiences import normalize_actions

from .experience_policy import DEFAULT_POLICY, digest, dump, rank_candidates


class ExperienceRuntime:
    def init_experience_schema(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS run_learning_context(
              run_id TEXT PRIMARY KEY REFERENCES runs(id), context_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT,
              request_key TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT NOT NULL,
              actual_action_id TEXT, actual_json TEXT, experience_id TEXT NOT NULL,
              experience_revision INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,request_key));
            CREATE TABLE IF NOT EXISTS memory_outbox(
              id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
              kind TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL,
              lease_token TEXT, lease_until REAL, last_error TEXT,
              receipt_json TEXT, depends_on TEXT REFERENCES memory_outbox(id), created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS memory_outbox_poll ON memory_outbox(state,available_at,lease_until);
            CREATE TABLE IF NOT EXISTS decision_evaluations(
              id TEXT PRIMARY KEY, decision_id TEXT NOT NULL REFERENCES decisions(id),
              request_hash TEXT NOT NULL, body_json TEXT NOT NULL, outbox_id TEXT NOT NULL);
            """)

    def prepare_learning_context(self, task):
        environment = task.get("environment", {})
        if not isinstance(environment, dict) or len(dump(environment)) > 8000:
            raise ValueError("environment must be an object up to 8000 characters")
        policy = dict(DEFAULT_POLICY)
        policy["mode"] = os.environ.get("COSENS_RSI_EXPERIENCE_MODE", "advisory")
        if policy["mode"] not in {"off", "shadow", "advisory"}:
            raise ValueError("experience mode must be off, shadow or advisory")
        for field, env, low, high in (("beta", "COSENS_RSI_EXPERIENCE_BETA", 0, 2),
                                      ("support_prior", "COSENS_RSI_EXPERIENCE_SUPPORT_PRIOR", 1, 1000),
                                      ("cost_weight", "COSENS_RSI_EXPERIENCE_COST_WEIGHT", 0, 1)):
            value = float(os.environ.get(env, policy[field]))
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"invalid experience policy {field}")
            policy[field] = value
        watermark = self._memory("GET", "/internal/v1/experiences/watermark")["watermark"]
        return {"schema": "rsi-learning-context-v1", "environment": environment,
                "watermark": watermark, "action_schema": "rsi-actions-v1",
                "reward_spec": "verified-decision-return-v1", "partition": "production",
                "policy": policy, "policy_version": digest(policy)}

    def _learning_context(self, run_id):
        with self._db() as db:
            row = db.execute("SELECT context_json FROM run_learning_context WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("legacy run has no frozen experience context; prepare a new run")
        return json.loads(row[0])

    def decision_guidance(self, run_id, principal, observation, candidates, node_id=None, request_key=None):
        run = self.get_run(run_id, principal)
        if run["status"] != "ready":
            raise ValueError("decisions require an active run")
        if not isinstance(observation, str) or len(observation) > 12000:
            raise ValueError("observation must be text up to 12000 characters")
        candidates = normalize_actions(candidates)
        context = self._learning_context(run_id)
        contract = {"deliverable": run["task_package"].get("deliverable", {}),
                    "acceptance": run["task_package"].get("acceptance", {})}
        if "recipe_compatibility" in context:
            contract["recipe_compatibility"] = context["recipe_compatibility"]
        if run["execution_mode"] == "guided":
            with self._db() as db:
                pending = db.execute("SELECT node_id FROM step_attempts WHERE run_id=? AND status='pending'",
                                     (run_id,)).fetchone()
                bundle = json.loads(db.execute("SELECT bundle_json FROM recipes WHERE id=?", (run["recipe_id"],)).fetchone()[0])
            if not pending or node_id != pending[0]:
                raise ValueError("guided decisions must bind to the current issued node")
            node = next(n for n in bundle["flow"]["nodes"] if n["id"] == node_id)
            step = bundle["steps"][bundle["bindings"][node["skill"]]]
            contract.update(inputs=step["inputs"], outputs=step["outputs"], checks=bundle["flow"]["checks"])
        elif node_id is not None:
            raise ValueError("assist decisions do not have a workflow node")
        request_hash = digest({"observation": observation, "candidates": candidates, "node_id": node_id,
                               "checkpoint": run["checkpoint"]})
        request_key = request_key or request_hash
        if not isinstance(request_key, str) or len(request_key) > 256:
            raise ValueError("decision request key must be text up to 256 characters")
        with self._db() as db:
            existing = db.execute("SELECT request_hash,response_json FROM decisions WHERE run_id=? AND request_key=?",
                                  (run_id, request_key)).fetchone()
        if existing:
            if existing[0] != request_hash:
                raise ValueError("decision idempotency key reused with different request")
            return json.loads(existing[1])
        phase = node_id or "assist"
        state = {"objective": run["task_package"]["objective"], "observation": observation, "phase": phase}
        contract_hash = digest(contract)
        search_body = {
            "task_family": run["task_package"]["task_family"], "phase": phase,
            "environment": context["environment"], "action_schema": context["action_schema"],
            "reward_spec": context["reward_spec"], "query": observation or state["objective"],
            "contract_hash": contract_hash,
            "watermark": context["watermark"], "limit": 50, "exclude_run_id": run_id}
        recalled = (self._memory("POST", "/internal/v1/experiences/search", search_body)
                    if context["policy"]["mode"] != "off" else
                    {"items": [], "watermark": context["watermark"], "profile": "experience-disabled"})
        decision = rank_candidates(candidates, recalled["items"], context["policy"])
        selected = next(s for s in decision["scores"] if s["action_id"] == decision["recommended_action_id"])
        avoided = any(s["q"] is not None and s["q"] < 0 for s in decision["scores"])
        decision["explanation"] = {"basis": "verified_compatible_experience" if selected["support"] else
                                   "avoided_negative_experience" if avoided else "base_preference",
                                   "q": selected["q"], "advantage": selected["advantage"],
                                   "independent_tasks": selected["independent_tasks"],
                                   "experience_refs": selected["experience_refs"],
                                   "scope": "current frozen Recipe and action semantics",
                                   "negative_evidence_action_ids": [s["action_id"] for s in decision["scores"]
                                                                    if s["q"] is not None and s["q"] < 0]}
        # Source knowledge remains a separate, cited context. Never turn
        # historical actions or outcome reports into high-priority instructions.
        extra = self._memory("POST", "/internal/v1/recall", {"query": observation or state["objective"],
                                                              "token_budget": 1000})
        did = "decision_" + uuid4().hex
        hints = {}
        if run.get("recipe_id"):
            with self._db() as db:
                frozen_recipe = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (run["recipe_id"],)).fetchone()
            frozen_bundle = json.loads(frozen_recipe[0])
            bindings = ([n["skill"] for n in frozen_bundle["flow"]["nodes"] if n["id"] == node_id]
                        if node_id else list(frozen_bundle["bindings"]))
            hints = {binding: frozen_bundle["steps"][frozen_bundle["bindings"][binding]]["body"] for binding in bindings}
        response = {"run_id": run_id, "decision_id": did, "execution_mode": run["execution_mode"],
                    "recipe_id": run["recipe_id"], "memory_snapshot_hash": run["snapshot_hash"],
                    "guidance": "Use the recommendation within the frozen node contract. Record the actual action and evidence; confidence scores are not calibrated probabilities.",
                    "state": state, "candidates": candidates, "decision": decision,
                    "execution_hints": {"source": "frozen_recipe", "methods": hints, "recommendation_only": True},
                    "additional_evidence": extra.get("items", []), "memory_context": run["snapshot"],
                    "experience_watermark": recalled["watermark"],
                    "contract_hash": contract_hash,
                    "retrieval_profile": recalled.get("profile"), "context_hash": ""}
        response["context_hash"] = digest(response)
        with self._db() as db:
            # Serialize against completion/cancellation and concurrent retries.
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if active[0] != "ready":
                raise ValueError("run is no longer active")
            if run["execution_mode"] == "guided" and not db.execute(
                    "SELECT 1 FROM step_attempts WHERE run_id=? AND node_id=? AND status='pending'",
                    (run_id, node_id)).fetchone():
                raise ValueError("workflow node is no longer active")
            previous = db.execute("SELECT request_hash,response_json FROM decisions WHERE run_id=? AND request_key=?",
                                  (run_id, request_key)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise ValueError("decision idempotency key reused with different request")
                return json.loads(previous[1])
            db.execute("""INSERT INTO decisions(id,run_id,node_id,request_key,request_hash,response_json,experience_id)
                VALUES(?,?,?,?,?,?,?)""", (did, run_id, node_id, request_key, request_hash, dump(response), "exp_" + did[9:]))
            self._event(db, run_id, "decision_recommended", {"decision_id": did,
                "recommended_action_id": decision["recommended_action_id"], "policy_version": context["policy_version"]})
        return response

    def _enqueue_memory(self, db, decision_id, kind, payload):
        predecessor = db.execute("SELECT id FROM memory_outbox WHERE decision_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                 (decision_id,)).fetchone()
        oid = "outbox_" + uuid4().hex
        db.execute("""INSERT INTO memory_outbox(id,decision_id,kind,payload_json,state,available_at,depends_on,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (oid, decision_id, kind, dump(payload), "queued", time.time(),
                                         predecessor[0] if predecessor else None, time.time()))
        return oid

    def _record_actual(self, db, run, decision_id, actual_action_id, next_state, *, node_id=None):
        decision = db.execute("SELECT * FROM decisions WHERE id=? AND run_id=?", (decision_id, run["run_id"])).fetchone()
        if not decision or decision["node_id"] != node_id:
            raise ValueError("decision does not belong to this run/node")
        guidance = json.loads(decision["response_json"])
        if actual_action_id not in {a["id"] for a in guidance["candidates"]}:
            raise ValueError("actual action must be a recorded candidate")
        if not isinstance(next_state, dict) or len(dump(next_state)) > 16000:
            raise ValueError("next_state must be an object up to 16000 characters")
        actual = {"actual_action_id": actual_action_id, "next_state": next_state}
        if decision["actual_action_id"]:
            if decision["actual_json"] != dump(actual):
                raise ValueError("actual action was already recorded with different data")
            return
        context = self._learning_context(run["run_id"])
        refs = []
        for item in (run["snapshot"].get("wiki", []) + run["snapshot"].get("evidence", [])
                     + guidance.get("additional_evidence", [])):
            for ref in item.get("refs", []):
                if ref not in refs:
                    refs.append(ref)
        if len(refs) > 32:
            raise ValueError("decision evidence exceeds 32 source references")
        record = {"run_id": run["run_id"], "decision_id": decision_id,
                  "task_lineage_id": run["task_package_hash"], "task_family": run["task_package"]["task_family"],
                  "phase": node_id or "assist", "environment": context["environment"],
                  "action_schema": context["action_schema"], "reward_spec": context["reward_spec"],
                  "partition": context["partition"], "state": guidance["state"],
                  "contract_hash": guidance["contract_hash"],
                  "candidates": guidance["candidates"], **actual, "source_refs": refs,
                  "recipe_id": run["recipe_id"], "recipe_revision": run["recipe_revision"],
                  "policy_version": context["policy_version"], "context_hash": guidance["context_hash"]}
        db.execute("UPDATE decisions SET actual_action_id=?,actual_json=? WHERE id=?",
                   (actual_action_id, dump(actual), decision_id))
        if context["policy"]["mode"] != "off":
            self._enqueue_memory(db, decision_id, "upsert", {"record": record,
                "experience_id": decision["experience_id"], "expected_revision": 0,
                "request_key": "experience:" + decision_id})
        self._event(db, run["run_id"], "actual_action_recorded", {"decision_id": decision_id,
            "actual_action_id": actual_action_id, "recommended_action_id": guidance["decision"]["recommended_action_id"]})

    def evaluate_decision(self, run_id, decision_id, evaluation, *, admin_token):
        if admin_token != self.admin_token:
            raise PermissionError("trusted evaluator credential required")
        required = {"evaluation_id", "reward_spec", "status", "evidence_artifact_id", "evidence_content_hash"}
        if not isinstance(evaluation, dict) or not required <= evaluation.keys() or len(dump(evaluation)) > 32000:
            raise ValueError("evaluation manifest is incomplete or too large")
        if (any(not isinstance(evaluation[k], str) or not evaluation[k].strip() for k in required)
                or evaluation["status"] not in {"verified", "inconclusive", "retracted"}):
            raise ValueError("invalid evaluation identity or status")
        value = evaluation.get("value")
        if evaluation["status"] == "verified" and (type(value) not in (int, float)
                or not math.isfinite(value) or not -1 <= value <= 1):
            raise ValueError("verified return must be finite and in [-1,1]")
        context = self._learning_context(run_id)
        if context["policy"]["mode"] == "off":
            raise ValueError("experience storage is disabled for this run")
        if evaluation["reward_spec"] != context["reward_spec"]:
            raise ValueError("reward specification differs from the frozen run")
        request_hash = digest({"decision_id": decision_id, "evaluation": evaluation})
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT request_hash,outbox_id FROM decision_evaluations WHERE id=?",
                                  (evaluation["evaluation_id"],)).fetchone()
            if previous:
                if previous[0] != request_hash:
                    raise ValueError("evaluation id reused with different data")
                return {"evaluation_id": evaluation["evaluation_id"], "outbox_id": previous[1], "status": "recorded"}
            decision = db.execute("""SELECT d.*,r.principal FROM decisions d JOIN runs r ON r.id=d.run_id
                WHERE d.id=? AND d.run_id=?""", (decision_id, run_id)).fetchone()
            if not decision or not decision["actual_action_id"]:
                raise ValueError("evaluation requires a recorded actual action")
            artifact = db.execute("SELECT content_hash FROM artifacts WHERE id=? AND principal=?",
                                  (evaluation["evidence_artifact_id"], decision["principal"])).fetchone()
            if not artifact or artifact[0] != evaluation["evidence_content_hash"]:
                raise ValueError("evaluation evidence artifact is unavailable or changed")
            outcome = {"evaluation_id": evaluation["evaluation_id"], "reward_spec": evaluation["reward_spec"],
                       "status": evaluation["status"], "value": value if evaluation["status"] == "verified" else None,
                       "evidence_ref": evaluation["evidence_artifact_id"], "evidence_content_hash": artifact[0]}
            oid = self._enqueue_memory(db, decision_id, "outcome", {"outcome": outcome,
                "request_key": "evaluation:" + evaluation["evaluation_id"]})
            db.execute("INSERT INTO decision_evaluations VALUES(?,?,?,?,?)",
                (evaluation["evaluation_id"], decision_id, request_hash, dump(evaluation), oid))
            self._event(db, run_id, "decision_evaluated", {"decision_id": decision_id,
                "evaluation_id": evaluation["evaluation_id"], "status": evaluation["status"]})
        return {"evaluation_id": evaluation["evaluation_id"], "outbox_id": oid, "status": "recorded"}

    def flush_memory_outbox(self, *, limit=10):
        delivered = 0
        for _ in range(limit):
            token = uuid4().hex
            current = time.time()
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""UPDATE memory_outbox SET state='failed',last_error='LeaseExpired',lease_token=NULL,lease_until=NULL
                    WHERE state='running' AND lease_until<? AND attempts>=5""", (current,))
                row = db.execute("""SELECT o.* FROM memory_outbox o WHERE
                    o.attempts<5 AND ((o.state IN ('queued','retry_wait') AND o.available_at<=?)
                     OR (o.state='running' AND o.lease_until<?))
                    AND (o.depends_on IS NULL OR EXISTS(SELECT 1 FROM memory_outbox p WHERE p.id=o.depends_on AND p.state='done'))
                    ORDER BY o.created_at,o.rowid LIMIT 1""", (current, current)).fetchone()
                if not row:
                    break
                payload = json.loads(row["payload_json"])
                decision = db.execute("SELECT experience_id,experience_revision FROM decisions WHERE id=?",
                                      (row["decision_id"],)).fetchone()
                if row["kind"] == "outcome" and "expected_revision" not in payload:
                    predecessor = db.execute("SELECT receipt_json FROM memory_outbox WHERE id=?", (row["depends_on"],)).fetchone()
                    payload["expected_revision"] = json.loads(predecessor[0])["revision"]
                db.execute("UPDATE memory_outbox SET state='running',lease_token=?,lease_until=?,attempts=attempts+1,payload_json=? WHERE id=?",
                           (token, current + 60, dump(payload), row["id"]))
            path = ("/internal/v1/experiences/upsert" if row["kind"] == "upsert" else
                    f"/internal/v1/experiences/{decision['experience_id']}/outcomes")
            try:
                receipt = self._memory("POST", path, payload)
            except Exception as exc:
                status_code = getattr(exc, "status_code", None)
                failed = row["attempts"] + 1 >= 5 or status_code in {400, 401, 403, 404, 409, 422}
                with self._db() as db:
                    db.execute("""UPDATE memory_outbox SET state=?,available_at=?,last_error=?,lease_token=NULL,lease_until=NULL
                        WHERE id=? AND lease_token=? AND state='running'""",
                        ("failed" if failed else "retry_wait", time.time() + min(60, 2 ** (row["attempts"] + 1)),
                         type(exc).__name__ + (":" + str(status_code) if status_code else ""), row["id"], token))
                continue
            with self._db() as db:
                changed = db.execute("""UPDATE memory_outbox SET state='done',receipt_json=?,payload_json='{}',
                    last_error=NULL,lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=? AND state='running'
                    AND lease_until>?""", (dump(receipt), row["id"], token, time.time()))
                if changed.rowcount:
                    db.execute("UPDATE decisions SET experience_revision=MAX(experience_revision,?) WHERE id=?",
                               (receipt["revision"], row["decision_id"]))
                    delivered += 1
        return {"delivered": delivered, "outbox": self.outbox_status()}

    def outbox_status(self):
        with self._db() as db:
            return {r["state"]: r["n"] for r in db.execute("SELECT state,COUNT(*) n FROM memory_outbox GROUP BY state")}

    def retry_memory_write(self, outbox_id, *, admin_token):
        if admin_token != self.admin_token:
            raise PermissionError("administrator credential required")
        with self._db() as db:
            changed = db.execute("""UPDATE memory_outbox SET state='queued',attempts=0,available_at=0,
                lease_token=NULL,lease_until=NULL,last_error=NULL WHERE id=? AND state='failed'""", (outbox_id,))
            if not changed.rowcount:
                raise ValueError("only failed Memory writes can be replayed")
        return {"outbox_id": outbox_id, "status": "queued"}

    def get_decision(self, run_id, principal, decision_id):
        self.get_run(run_id, principal)
        with self._db() as db:
            row = db.execute("SELECT * FROM decisions WHERE id=? AND run_id=?", (decision_id, run_id)).fetchone()
            if not row:
                raise LookupError("decision unavailable")
            writes = [dict(r) for r in db.execute(
                "SELECT id,kind,state,attempts,last_error,receipt_json FROM memory_outbox WHERE decision_id=? ORDER BY created_at,rowid",
                (decision_id,))]
        return {"decision_id": decision_id, "recommendation": json.loads(row["response_json"])["decision"],
                "actual": json.loads(row["actual_json"]) if row["actual_json"] else None,
                "experience": {"experience_id": row["experience_id"], "revision": row["experience_revision"]},
                "writes": writes}
