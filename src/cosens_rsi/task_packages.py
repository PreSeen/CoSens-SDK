"""Immutable replay packages compiled from authorized service evidence.

The catalog stores replay metadata, not another factual Memory. Capture events
remain lossless and idempotent; dashboard traces are never promoted to complete
action telemetry. Final deliverables cannot replace missing initial inputs.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from pathlib import Path

from .algorithms.hierarchical import fingerprint
from .evaluation import _safe_path

WAITING = (
    "awaiting_input",
    "awaiting_runner",
    "awaiting_judge",
    "awaiting_validation_tasks",
)


class TaskPackages:
    def init_task_package_schema(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS generic_schema_versions(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);
            INSERT OR IGNORE INTO generic_schema_versions VALUES(1, strftime('%s','now'));
            CREATE TABLE IF NOT EXISTS canonical_session_events(
              run_id TEXT NOT NULL REFERENCES runs(id), event_id TEXT NOT NULL,
              event_hash TEXT NOT NULL, body_json TEXT NOT NULL, received_at REAL NOT NULL,
              PRIMARY KEY(run_id,event_id));
            CREATE TABLE IF NOT EXISTS native_capture_cursors(
              principal TEXT NOT NULL, source_key TEXT NOT NULL, identity TEXT NOT NULL,
              byte_cursor INTEGER NOT NULL, thread_id TEXT NOT NULL, run_id TEXT,
              PRIMARY KEY(principal,source_key));
            CREATE TABLE IF NOT EXISTS replay_packages(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), principal TEXT NOT NULL,
              project TEXT NOT NULL, family TEXT NOT NULL, lineage TEXT NOT NULL,
              content_hash TEXT NOT NULL, package_json TEXT NOT NULL, created_at REAL NOT NULL,
              UNIQUE(run_id,content_hash));
            CREATE INDEX IF NOT EXISTS replay_packages_scope ON replay_packages(principal,project,family);
            CREATE TABLE IF NOT EXISTS replay_input_supplements(
              run_id TEXT NOT NULL REFERENCES runs(id), artifact_id TEXT NOT NULL REFERENCES artifacts(id),
              created_at REAL NOT NULL, PRIMARY KEY(run_id,artifact_id));
            CREATE TABLE IF NOT EXISTS experiment_exposures(
              principal TEXT NOT NULL, project TEXT NOT NULL, identity TEXT NOT NULL,
              job_id TEXT NOT NULL, role TEXT NOT NULL, candidate_sha TEXT NOT NULL,
              created_at REAL NOT NULL, PRIMARY KEY(principal,project,identity));
            CREATE TABLE IF NOT EXISTS generic_improvement_jobs(
              job_id TEXT PRIMARY KEY REFERENCES jobs(id), checkpoint_json TEXT NOT NULL DEFAULT '{}',
              attempt INTEGER NOT NULL DEFAULT 0, stage TEXT NOT NULL DEFAULT 'preparing');
            CREATE TABLE IF NOT EXISTS generic_evidence_generations(
              principal TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            """)

    def _wake_generic(self, db, principal):
        db.execute(
            "INSERT INTO generic_evidence_generations VALUES(?,1) ON CONFLICT(principal) "
            "DO UPDATE SET generation=generation+1",
            (principal,),
        )
        placeholders = ",".join("?" for _ in WAITING)
        db.execute(
            f"UPDATE jobs SET state='queued' WHERE state IN ({placeholders}) AND id IN "
            "(SELECT g.job_id FROM generic_improvement_jobs g JOIN harness_jobs h ON h.job_id=g.job_id "
            "WHERE h.principal=?)",
            (*WAITING, principal),
        )

    def capture_session_events(self, run_id, principal, events):
        self._authorize_evidence_write(run_id, principal)
        if not isinstance(events, list) or not events or len(events) > 200:
            raise ValueError("capture requires 1..200 events")
        raw = json.dumps(events, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 1_000_000:
            raise ValueError("capture batch exceeds 1 MB")
        prepared = []
        for e in events:
            if (
                not isinstance(e, dict)
                or not isinstance(e.get("id"), str)
                or not e["id"]
                or len(e["id"]) > 256
                or e.get("kind")
                not in {
                    "user_prompt",
                    "agent_action",
                    "tool_result",
                    "context_delivery",
                    "correction",
                    "artifact",
                    "session_end",
                }
                or not isinstance(e.get("payload"), dict)
            ):
                raise ValueError("invalid canonical event")
            # API events carry observable evidence, not trusted completeness,
            # expert identity or authority to execute server-side code.
            prepared.append(
                (e["id"], fingerprint(e), json.dumps(e, ensure_ascii=False))
            )
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            inserted = 0
            for eid, digest, body in prepared:
                old = db.execute(
                    "SELECT event_hash FROM canonical_session_events WHERE run_id=? AND event_id=?",
                    (run_id, eid),
                ).fetchone()
                if old and old[0] != digest:
                    raise ValueError("capture event ID reused with different content")
                inserted += db.execute(
                    "INSERT OR IGNORE INTO canonical_session_events VALUES(?,?,?,?,?)",
                    (run_id, eid, digest, body, time.time()),
                ).rowcount
            if inserted:
                self._wake_generic(db, principal)
        return {
            "run_id": run_id,
            "durable_ack": [p[0] for p in prepared],
            "coverage": "observable_events",
        }

    def _authorize_evidence_write(self, run_id, principal):
        # Durable capture/corrections must work while Memory is offline. This
        # authorizes the write without returning any stale source contents.
        with self._db() as db:
            if not db.execute(
                "SELECT 1 FROM runs WHERE id=? AND principal=?", (run_id, principal)
            ).fetchone():
                raise LookupError("run not found")

    def supply_initial_inputs(self, run_id, principal, artifact_ids):
        """Supplement missing materials explicitly; never rewrite a frozen run.

        These are labelled supplied materials, not proof of the historical
        filesystem. A job already executing must start a new experiment.
        """
        self._authorize_evidence_write(run_id, principal)
        if (
            not isinstance(artifact_ids, list)
            or not artifact_ids
            or len(artifact_ids) > 100
            or any(not isinstance(a, str) for a in artifact_ids)
            or len(set(artifact_ids)) != len(artifact_ids)
        ):
            raise ValueError("unique initial input artifact IDs required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            checkpoints = db.execute(
                "SELECT g.checkpoint_json FROM generic_improvement_jobs g JOIN jobs j ON j.id=g.job_id "
                "WHERE j.resource_id=?",
                (run_id,),
            ).fetchall()
            if any(json.loads(r[0]).get("cases") for r in checkpoints):
                raise ValueError(
                    "inputs already frozen; prepare a new run with the new material version"
                )
            for aid in artifact_ids:
                if not db.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND principal=?",
                    (aid, principal),
                ).fetchone():
                    raise LookupError("initial input unavailable")
                db.execute(
                    "INSERT OR IGNORE INTO replay_input_supplements VALUES(?,?,?)",
                    (run_id, aid, time.time()),
                )
            self._event(
                db,
                run_id,
                "initial_inputs_supplemented",
                {"artifact_ids": artifact_ids, "author": principal},
            )
            self._wake_generic(db, principal)
        return {
            "run_id": run_id,
            "status": "supplied",
            "basis": "supplemented_materials_not_historical_snapshot",
        }

    def _compile_package(self, run_id, principal):
        run = self.get_run(run_id, principal)
        task = run["task_package"]
        inputs, refs, gaps = {}, [], []
        ids = list(task.get("input_artifact_ids", []))
        with self._db() as db:
            supplements = [
                r[0]
                for r in db.execute(
                    "SELECT artifact_id FROM replay_input_supplements WHERE run_id=? ORDER BY artifact_id",
                    (run_id,),
                )
            ]
        ids = list(dict.fromkeys(ids + supplements))
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or len(ids) > 100:
            raise ValueError("initial inputs require unique artifact references")
        with self._db() as db:
            for aid in ids:
                row = db.execute(
                    "SELECT * FROM artifacts WHERE id=? AND principal=?",
                    (aid, principal),
                ).fetchone()
                if not row:
                    raise LookupError("initial input unavailable")
                if row["content_hash"] != "sha256:" + fingerprint(row["content"]):
                    raise ValueError("immutable input artifact changed")
                _safe_path(Path("/rsi-input-root"), row["name"])
                if row["name"] in inputs:
                    raise ValueError("ambiguous initial input filenames")
                inputs[row["name"]] = row["content"]
                refs.append(
                    {"id": aid, "sha256": row["content_hash"], "path": row["name"]}
                )
            events = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body_json FROM canonical_session_events WHERE run_id=? ORDER BY received_at,event_id",
                    (run_id,),
                )
            ]
            feedback = [
                {
                    "id": r["id"],
                    "sha256": fingerprint(json.loads(r["body_json"])),
                    "body": json.loads(r["body_json"]),
                    "author": principal,
                    "authority": "run_owner",
                }
                for r in db.execute(
                    "SELECT * FROM feedback WHERE run_id=? ORDER BY created_at,id",
                    (run_id,),
                )
            ]
        # The initial task may be wholly self-contained in its prompt. Declared
        # file inputs, however, must have immutable content, never a local path
        # or a final answer guessed to be the original input.
        deliverable = task.get("deliverable", {})
        if not isinstance(deliverable, dict) or not isinstance(
            task.get("acceptance", {}), dict
        ):
            raise ValueError("deliverable and acceptance must be objects")  # noqa: TRY004
        if deliverable.get("requires_files") and not inputs:
            gaps.append("missing_initial_inputs")
        lineage = fingerprint({"task": task["objective"], "inputs": inputs})
        package = {
            "schema": "replay-package-v1",
            "source_run_id": run_id,
            "principal": principal,
            "project": self.memory_project,
            "family": task["task_family"],
            "lineage": lineage,
            "declared_lineage_id": task.get("task_lineage_id"),
            "task": task["objective"],
            "task_hash": run["task_package_hash"],
            "inputs": inputs,
            "input_refs": refs,
            "acceptance": deepcopy(task.get("acceptance", {})),
            "input_basis": "supplemented_materials"
            if supplements
            else "declared_initial_materials",
            "deliverable": deepcopy(deliverable),
            "environment": task.get("environment", {}),
            "snapshot": run["snapshot"],
            "snapshot_hash": run["snapshot_hash"],
            "events": events,
            "feedback": feedback,
            "readiness": "hypothesis_only" if gaps else "replayable",
            "gaps": gaps,
        }
        digest = fingerprint(package)
        package["id"] = "package_" + digest
        package["sha256"] = digest
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO replay_packages VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    package["id"],
                    run_id,
                    principal,
                    self.memory_project,
                    task["task_family"],
                    lineage,
                    digest,
                    json.dumps(package, ensure_ascii=False, allow_nan=False),
                    time.time(),
                ),
            )
        return package

    def collect_task_packages(self, run_id, principal):
        source = self._compile_package(run_id, principal)
        # Historical backfill is automatic and scoped; no business-specific
        # cases.json or partition assignment can be submitted by a caller.
        with self._db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM runs WHERE principal=? AND project=? AND family=? "
                    "ORDER BY created_at,id",
                    (principal, self.memory_project, source["family"]),
                )
            ]
        packages = [source]
        for rid in ids:
            if rid != run_id:
                try:
                    packages.append(self._compile_package(rid, principal))
                except (LookupError, ValueError):
                    # An unavailable historical example does not block a new
                    # authorized run. It cannot become an evaluation task.
                    continue
        unique = {source["lineage"]: source}
        for p in packages[1:]:
            unique.setdefault(p["lineage"], p)
        return source, list(unique.values())

    @staticmethod
    def _exposure_keys(package):
        # A new run, family, acceptance wording or job ID cannot clear actual
        # prompt/input exposure. Reused input artifacts conservatively share
        # a lineage until a trusted task-specific splitter exists.
        if "exposure_keys" in package:
            return set(package["exposure_keys"])
        keys = {
            "task:" + package["lineage"],
            "prompt:" + fingerprint(package["task"]),
            *("input:" + fingerprint(v) for v in package["inputs"].values()),
        }
        if package.get("declared_lineage_id"):
            keys.add("declared-lineage:" + package["declared_lineage_id"])
        return keys

    def _package_unexposed(self, package, job_id):
        with self._db() as db:
            rows = db.execute(
                "SELECT identity,job_id,role FROM experiment_exposures WHERE principal=? AND project=?",
                (package["principal"], package["project"]),
            ).fetchall()
        keys = self._exposure_keys(package)
        return not any(r["identity"] in keys for r in rows)

    def _reserve_exposure(self, packages, job_id, role, candidate_sha):
        """All-or-nothing reservation before any role receives task content."""
        if role not in {"train", "screen", "confirm"}:
            raise ValueError("invalid exposure role")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            for p in packages:
                for key in self._exposure_keys(p):
                    old = db.execute(
                        "SELECT job_id,role,candidate_sha FROM experiment_exposures "
                        "WHERE principal=? AND project=? AND identity=?",
                        (p["principal"], p["project"], key),
                    ).fetchone()
                    if old:
                        if tuple(old) == (job_id, role, candidate_sha):
                            continue  # The two arms of a locked pair share a reservation.
                        if role == "train" and old["role"] == "train":
                            continue  # Relearning from old training evidence is allowed.
                        if role == "screen" and old["role"] == "screen":
                            continue  # A selection set is never fresh confirmation evidence.
                        raise ValueError("task evidence has already been exposed")
                    db.execute(
                        "INSERT INTO experiment_exposures VALUES(?,?,?,?,?,?,?)",
                        (
                            p["principal"],
                            p["project"],
                            key,
                            job_id,
                            role,
                            candidate_sha,
                            time.time(),
                        ),
                    )

    def _validate_package(self, package):
        if (
            fingerprint({k: v for k, v in package.items() if k not in {"id", "sha256"}})
            != package["sha256"]
        ):
            raise ValueError("immutable replay package changed")
        principal = package["principal"]
        run = self.get_run(package["source_run_id"], principal)
        if (
            run["task_package_hash"] != package["task_hash"]
            or run["snapshot_hash"] != package["snapshot_hash"]
            or "sha256:" + fingerprint(run["task_package"]) != package["task_hash"]
            or "sha256:" + fingerprint(run["snapshot"]) != package["snapshot_hash"]
        ):
            raise ValueError("frozen task context changed")
        with self._db() as db:
            for ref in package["input_refs"]:
                row = db.execute(
                    "SELECT content_hash,content,name FROM artifacts WHERE id=? AND principal=?",
                    (ref["id"], principal),
                ).fetchone()
                if (
                    not row
                    or row[0] != ref["sha256"]
                    or row[0] != "sha256:" + fingerprint(row[1])
                    or row[2] != ref["path"]
                ):
                    raise LookupError("frozen input withdrawn or changed")
            current_feedback = {
                r[0]
                for r in db.execute(
                    "SELECT id FROM feedback WHERE run_id=?",
                    (package["source_run_id"],),
                )
            }
            if current_feedback != {f["id"] for f in package["feedback"]}:
                raise LookupError("new correction requires a new frozen experiment")
            for f in package["feedback"]:
                row = db.execute(
                    "SELECT body_json FROM feedback WHERE id=? AND run_id=?",
                    (f["id"], package["source_run_id"]),
                ).fetchone()
                if not row or fingerprint(json.loads(row[0])) != f["sha256"]:
                    raise LookupError("frozen correction withdrawn or changed")
        return True
