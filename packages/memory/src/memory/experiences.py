"""Versioned episodic memory. Experiences never enter chunks, Wiki or compilation.

Memory owns persistence, ACL and live evidence checks. The calling RSI service
owns reward evaluation and policy estimation. Only a trusted host evaluator may
mark an outcome verified; ordinary client feedback cannot do so.
"""
from __future__ import annotations

import hashlib
import json
import math
from uuid import uuid4

from .models import AccessDenied, Conflict, InvalidEvidence, SourceRef
from .service import terms


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def action_signature(action):
    return fingerprint({k: action[k] for k in ("id", "version", "description")})


def normalize_actions(actions):
    if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
        raise ValueError("provide 1..8 candidate actions")
    result, seen = [], set()
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("action must be an object")
        key = action.get("id")
        version = action.get("version", "1")
        description = action.get("description")
        if (not isinstance(key, str) or not key.strip() or len(key) > 128 or key in seen
                or not isinstance(version, str) or not version.strip() or len(version) > 128
                or not isinstance(description, str) or not description.strip() or len(description) > 4000):
            raise ValueError("actions need unique ids, versions and descriptions")
        confidence = action.get("confidence", 0.5)
        cost = action.get("cost", 0.0)
        if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 1
               for x in (confidence, cost)):
            raise ValueError("confidence and normalized cost must be finite values in [0,1]")
        result.append({"id": key, "version": version, "description": description,
                       "confidence": float(confidence), "cost": float(cost)})
        seen.add(key)
    return result


class ExperienceService:
    def __init__(self, memory):
        self.memory = memory
        self.db = memory.db

    def _head(self, actor, project, experience_id, *, write=False):
        head = self.db.one("SELECT * FROM experiences WHERE tenant=? AND project=? AND id=?",
                           (actor.tenant, project, experience_id))
        if (not head or head["state"] != "active"
                or (json.loads(head["readers"]) and actor.principal not in json.loads(head["readers"]))):
            raise AccessDenied("experience unavailable")
        if write and head["owner"] != actor.principal:
            raise AccessDenied("only the experience owner may revise it")
        return head

    def _validate_sources(self, actor, project, refs):
        if not isinstance(refs, list) or len(refs) > 32:
            raise ValueError("source_refs must contain at most 32 references")
        parsed = []
        for ref in refs:
            try:
                item = SourceRef(**ref)
            except (TypeError, KeyError) as exc:
                raise ValueError("invalid source reference") from exc
            if any(type(v) is not int for v in (item.revision, item.start, item.end)):
                raise ValueError("invalid source reference offsets or revision")
            parsed.append(item)
        if parsed:
            self.memory._validate_refs(actor, project, tuple(parsed))

    def _validate_record(self, record):
        if not isinstance(record, dict):
            raise ValueError("experience record must be an object")
        record = json.loads(canonical(record))
        if len(canonical(record)) > 128000:
            raise ValueError("experience exceeds 128000 characters")
        for field in ("run_id", "task_family", "phase", "action_schema", "reward_spec"):
            if not isinstance(record.get(field), str) or not record[field].strip() or len(record[field]) > 256:
                raise ValueError(f"{field} is required and limited to 256 characters")
        if not isinstance(record.get("state"), dict) or not isinstance(record.get("environment", {}), dict):
            raise ValueError("state and environment must be objects")
        record["environment"] = record.get("environment", {})
        record["contract_hash"] = record.get("contract_hash", "unscoped")
        if not isinstance(record["contract_hash"], str) or not record["contract_hash"]:
            raise ValueError("contract_hash must be nonempty text")
        record["candidates"] = normalize_actions(record.get("candidates"))
        action = next((a for a in record["candidates"] if a["id"] == record.get("actual_action_id")), None)
        if not action:
            raise ValueError("actual action must belong to the recorded candidate set")
        record["action"] = {k: action[k] for k in ("id", "version", "description")}
        record["action_signature"] = action_signature(action)
        record["partition"] = record.get("partition", "production")
        if not isinstance(record["partition"], str) or record["partition"] not in {"production", "train", "screen", "confirm", "held_out"}:
            raise ValueError("invalid experience partition")
        record["source_refs"] = record.get("source_refs", [])
        # Replacing an observation always invalidates its previous evaluation.
        record["outcome"] = {"status": "pending", "value": None}
        return record

    def _receipt(self, actor, project, key, digest):
        row = self.db.one("SELECT * FROM experience_receipts WHERE tenant=? AND project=? AND principal=? AND request_key=?",
                          (actor.tenant, project, actor.principal, fingerprint(key)))
        if row:
            if row["fingerprint"] != digest:
                raise Conflict("idempotency key reused with different experience request")
            result = json.loads(row["result"])
            self._head(actor, project, result["experience_id"])
            return result

    def _save(self, actor, project, experience_id, revision, record, key, digest):
        sequence = self.memory._sequence(actor.tenant, project)
        content_hash = fingerprint(record)
        self.db.execute("""INSERT INTO experience_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (actor.tenant, project, experience_id, revision, sequence, record["task_family"], record["phase"],
             fingerprint(record["environment"]), record["action_schema"], record["reward_spec"], record["partition"],
             record["outcome"]["status"], canonical(record), content_hash, self.memory.clock()))
        self.db.execute("UPDATE experiences SET revision=? WHERE tenant=? AND project=? AND id=?",
                        (revision, actor.tenant, project, experience_id))
        result = {"experience_id": experience_id, "revision": revision, "content_hash": content_hash,
                  "watermark": sequence, "reward_status": record["outcome"]["status"]}
        self.db.execute("INSERT INTO experience_receipts VALUES(?,?,?,?,?,?)",
            (actor.tenant, project, actor.principal, fingerprint(key), digest, canonical(result)))
        return result

    def watermark(self, actor, project):
        with self.db.transaction():
            self.memory._scope(actor, project)
            return self.db.one("SELECT sequence FROM projects WHERE tenant=? AND id=?",
                               (actor.tenant, project))["sequence"]

    def upsert(self, actor, project, record, *, request_key, experience_id=None, expected_revision=0, readers=()):
        if not isinstance(request_key, str) or not request_key or type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("request key and nonnegative expected_revision required")
        if experience_id is not None and (not isinstance(experience_id, str) or not experience_id or len(experience_id) > 256):
            raise ValueError("invalid experience id")
        record = self._validate_record(record)
        if not isinstance(readers, (list, tuple)) or any(not isinstance(r, str) or not r for r in readers):
            raise ValueError("readers must contain principal identifiers")
        readers = sorted(set(readers))
        if readers and actor.principal not in readers:
            raise AccessDenied("writer must be able to read experience")
        digest = fingerprint(["upsert", record, experience_id, expected_revision, readers])
        with self.db.transaction():
            self.memory._scope(actor, project, write=True)
            previous = self._receipt(actor, project, request_key, digest)
            if previous:
                return previous
            self._validate_sources(actor, project, record["source_refs"])
            experience_id = experience_id or "exp_" + uuid4().hex
            head = self.db.one("SELECT * FROM experiences WHERE tenant=? AND project=? AND id=?",
                               (actor.tenant, project, experience_id))
            if head:
                head = self._head(actor, project, experience_id, write=True)
                if head["revision"] != expected_revision or json.loads(head["readers"]) != readers:
                    raise Conflict("experience revision or ACL changed")
            elif expected_revision:
                raise Conflict("experience does not exist")
            else:
                self.db.execute("INSERT INTO experiences VALUES(?,?,?,?,?,?,?)",
                    (actor.tenant, project, experience_id, 0, "active", actor.principal, canonical(readers)))
            return self._save(actor, project, experience_id, expected_revision + 1, record, request_key, digest)

    def get(self, actor, project, experience_id, *, revision=None):
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("revision must be a positive integer")
        with self.db.transaction():
            self.memory._scope(actor, project)
            head = self._head(actor, project, experience_id)
            row = self.db.one("SELECT * FROM experience_versions WHERE tenant=? AND project=? AND id=? AND revision=?",
                (actor.tenant, project, experience_id, revision or head["revision"]))
            if not row:
                raise LookupError("experience revision missing")
            record = json.loads(row["payload"])
            self._validate_sources(actor, project, record["source_refs"])
            self._validate_sources(actor, project, record["outcome"].get("source_refs", []))
            return {"experience_id": experience_id, "revision": row["revision"], "watermark": row["sequence"],
                    "content_hash": row["content_hash"], "record": record,
                    "is_current_revision": row["revision"] == head["revision"],
                    "eligible_for_runtime": row["revision"] == head["revision"]
                    and record["outcome"]["status"] == "verified"
                    and record["partition"] in {"production", "train"}}

    def append_outcome(self, actor, project, experience_id, outcome, *, request_key, expected_revision):
        if not actor.administrator:
            raise AccessDenied("reward verification requires a trusted evaluator")
        if not isinstance(request_key, str) or not request_key or type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("request key and expected_revision required")
        outcome = json.loads(canonical(outcome))
        if (not isinstance(outcome, dict) or len(canonical(outcome)) > 32000
                or not isinstance(outcome.get("status"), str)
                or outcome["status"] not in {"verified", "retracted", "inconclusive"}):
            raise ValueError("invalid outcome status or size")
        if not all(isinstance(outcome.get(k), str) and outcome[k].strip()
                   for k in ("evaluation_id", "reward_spec", "evidence_ref")):
            raise ValueError("evaluation_id, reward_spec and evidence_ref required")
        if outcome["status"] == "verified":
            value = outcome.get("value")
            if type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1:
                raise ValueError("verified return must be finite and in [-1,1]")
        else:
            outcome["value"] = None
        digest = fingerprint(["outcome", experience_id, expected_revision, outcome])
        with self.db.transaction():
            self.memory._scope(actor, project, write=True)
            previous = self._receipt(actor, project, request_key, digest)
            if previous:
                return previous
            head = self._head(actor, project, experience_id, write=True)
            if head["revision"] != expected_revision:
                raise Conflict("experience revision changed")
            row = self.db.one("SELECT payload FROM experience_versions WHERE tenant=? AND project=? AND id=? AND revision=?",
                             (actor.tenant, project, experience_id, expected_revision))
            record = json.loads(row["payload"])
            if record["reward_spec"] != outcome["reward_spec"]:
                raise Conflict("reward specification changed")
            self._validate_sources(actor, project, record["source_refs"])
            self._validate_sources(actor, project, outcome.get("source_refs", []))
            record["outcome"] = outcome
            return self._save(actor, project, experience_id, expected_revision + 1, record, request_key, digest)

    def withdraw(self, actor, project, experience_id):
        with self.db.transaction():
            self.memory._scope(actor, project, write=True)
            self._head(actor, project, experience_id, write=True)
            self.db.execute("UPDATE experiences SET state='withdrawn' WHERE tenant=? AND project=? AND id=?",
                            (actor.tenant, project, experience_id))
            self.memory._sequence(actor.tenant, project)

    def set_readers(self, actor, project, experience_id, readers):
        if not isinstance(readers, (list, tuple)) or any(not isinstance(r, str) or not r for r in readers):
            raise ValueError("readers must contain principal identifiers")
        acl = sorted(set(readers))
        if acl and actor.principal not in acl:
            raise AccessDenied("owner must retain access")
        with self.db.transaction():
            self.memory._scope(actor, project, write=True)
            self._head(actor, project, experience_id, write=True)
            self.db.execute("UPDATE experiences SET readers=? WHERE tenant=? AND project=? AND id=?",
                            (canonical(acl), actor.tenant, project, experience_id))
            self.memory._sequence(actor.tenant, project)

    def search(self, actor, project, *, task_family, phase, environment, action_schema, reward_spec,
               query, watermark=None, limit=10, exclude_run_id=None, partitions=("production", "train"),
               contract_hash="unscoped"):
        if (not partitions or set(partitions) - {"production", "train", "screen", "confirm", "held_out"}
                or (set(partitions) - {"production", "train"} and not actor.administrator)):
            raise AccessDenied("experience partition unavailable")
        if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(query, str) or len(query) > 20000:
            raise ValueError("invalid search query or limit")
        if watermark is not None and (type(watermark) is not int or watermark < 0):
            raise ValueError("invalid experience watermark")
        with self.db.transaction():
            self.memory._scope(actor, project)
            current = self.db.one("SELECT sequence FROM projects WHERE tenant=? AND id=?",
                                  (actor.tenant, project))["sequence"]
            cutoff = current if watermark is None else min(watermark, current)
            marks = ",".join("?" for _ in partitions)
            rows = self.db.all(f"""SELECT v.* FROM experience_versions v JOIN experiences h
                ON h.tenant=v.tenant AND h.project=v.project AND h.id=v.id AND h.revision=v.revision
                WHERE v.tenant=? AND v.project=? AND h.state='active' AND v.family=? AND v.phase=?
                AND v.environment_hash=? AND v.action_schema=? AND v.reward_spec=?
                AND v.partition_name IN ({marks}) AND v.reward_status='verified' AND v.sequence<=?
                ORDER BY v.sequence DESC LIMIT 2000""",
                (actor.tenant, project, task_family, phase, fingerprint(environment), action_schema,
                 reward_spec, *partitions, cutoff))
            found = []
            query_terms = terms(query)
            for row in rows:
                try:
                    self._head(actor, project, row["id"])
                    record = json.loads(row["payload"])
                    if record["run_id"] == exclude_run_id or record.get("contract_hash", "unscoped") != contract_hash:
                        continue
                    self._validate_sources(actor, project, record["source_refs"])
                    self._validate_sources(actor, project, record["outcome"].get("source_refs", []))
                except (AccessDenied, InvalidEvidence):
                    continue
                state_text = record["state"].get("observation") or record["state"].get("objective")
                state_terms = terms(state_text if isinstance(state_text, str) else canonical(record["state"]))
                similarity = len(query_terms & state_terms) / max(1, len(query_terms | state_terms))
                if not query_terms or similarity < 0.1:
                    continue
                found.append({"experience_id": row["id"], "revision": row["revision"],
                              "content_hash": row["content_hash"], "similarity": similarity, "record": record})
            found.sort(key=lambda item: (-item["similarity"], item["experience_id"]))
            return {"items": found[:limit], "watermark": cutoff, "scanned": len(rows),
                    "scan_truncated": len(rows) == 2000, "profile": "structured-experience-lexical-v1"}
