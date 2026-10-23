"""Persistent, family-independent preparation around the existing learner.

Only declarative Recipe candidates evolve. The fixed runner, external checks,
paired comparisons, fresh confirmation and explicit publication stay separate.
Waiting resumes with the same candidates; it never relearns from confirm data.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from .algorithms.hierarchical import fingerprint, seed_bundle
from .algorithms.hierarchical_learning import (
    PROPOSER_INSTRUCTION,
    BundleRegistry,
    HierarchicalLearner,
    _screen_survives,
    compare,
    confirm_decision,
    prepare_portfolio,
)
from .algorithms.trajectory import diagnose
from .evaluation import FrozenEvaluationSession
from .evaluation_specs import EvaluationNotReady, build_spec, evaluate_spec


class GenericImprovement:
    def _validate_generic_release(self, job_id, checkpoint):
        manifest = checkpoint.get("manifest", {})
        ids = manifest.get("partitions", {}).get("confirm", [])
        decision = checkpoint.get("release_decision", {})
        pairs = decision.get("comparison", {}).get("pairs", [])
        candidate = decision.get("candidate_sha256")
        if (
            len(ids) < checkpoint["policy"]["min_confirm_tasks"]
            or len(ids) != len(set(ids))
            or {p.get("task_id") for p in pairs} != set(ids)
            or len(pairs) != len(ids)
        ):
            raise ValueError(
                "release requires the complete independent confirmation manifest"
            )
        packages = {p["id"]: p for p in checkpoint["packages"]}
        with self._db() as db:
            for cid in ids:
                package = packages[cid]
                for key in self._exposure_keys(package):
                    receipt = db.execute(
                        "SELECT job_id,role,candidate_sha FROM experiment_exposures "
                        "WHERE principal=? AND project=? AND identity=?",
                        (package["principal"], package["project"], key),
                    ).fetchone()
                    if not receipt or tuple(receipt) != (job_id, "confirm", candidate):
                        raise ValueError(
                            "release confirmation exposure receipt unavailable"
                        )
        root = Path(self.db_path).resolve().parent / "harness-experiments" / job_id
        digest = manifest.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("invalid release manifest hash")
        path = root / "generations" / manifest["sha256"] / "frozen-session.json"
        try:
            frozen = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError("release frozen session unavailable") from None
        if (
            frozen.get("sha256") != manifest["sha256"]
            or frozen["cases"] != checkpoint["cases"]
            or fingerprint({"cases": frozen["cases"], "protocol": frozen["protocol"]})
            != manifest["sha256"]
        ):
            raise ValueError("release frozen session integrity mismatch")

    def _generic_checkpoint(self, job_id, checkpoint, stage):
        with self._db() as db:
            db.execute(
                "UPDATE generic_improvement_jobs SET checkpoint_json=?,stage=? WHERE job_id=?",
                (
                    json.dumps(checkpoint, ensure_ascii=False, allow_nan=False),
                    stage,
                    job_id,
                ),
            )

    def _generic_report(self, job_id, state, cp, *, reason=None):
        rounds = cp.get("rounds", [])
        candidates = cp.get("candidates", [])
        if not rounds and candidates:
            rounds = [
                {
                    "round": 1,
                    "base_sha256": fingerprint(cp["initial"]),
                    "diagnosis": cp.get("diagnosis", {}),
                    "diagnostic_context": cp.get("diagnostic", {}),
                    "candidates": [
                        {k: v for k, v in c.items() if k != "bundle"}
                        for c in candidates
                    ],
                    "decision": {
                        "status": state,
                        "reason": reason,
                        "candidate_sha256": cp.get("winner_sha"),
                    },
                }
            ]
        report = {
            "schema": "generic-improvement-v1",
            "mode": "generic",
            "stage": state,
            "reason": reason,
            "base_recipe_id": cp.get("base_recipe_id") or None,
            "base_sha256": fingerprint(cp["initial"]) if "initial" in cp else None,
            "baseline_kind": "production_recipe"
            if cp.get("base_recipe_id")
            else "private_unvalidated_seed",
            "evidence_level": "transfer_validated"
            if state in {"validated", "published"}
            else "candidate_ready"
            if candidates
            else "hypothesis_only",
            "preparation": {
                "task_packages": len(cp.get("cases", [])),
                "candidate_count": len(candidates),
                "hypothesis_count": len(cp.get("diagnostic", {}).get("cards", [])),
                "missing": reason,
                "context_scope": "frozen_experiment_view_no_production_recall",
            },
            "policy": cp.get("policy"),
            "manifest": cp.get("manifest", {}),
            "manifest_generations": cp.get("manifest_generations", []),
            "result": {"active": cp.get("active", cp.get("initial")), "rounds": rounds},
            "release_decision": cp.get("release_decision", {}),
            "runtime_sha256": cp.get("runtime_sha256"),
            "checkpoint_sha256": fingerprint(cp),
        }
        from .task_packages import WAITING

        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT generation FROM generic_evidence_generations WHERE principal=?",
                (cp.get("principal"),),
            ).fetchone()
            # Material arriving while a worker was preparing cannot lose its
            # wakeup when that worker subsequently transitions to waiting.
            persisted_state = (
                "queued"
                if state in WAITING
                and current
                and current[0] != cp.get("observed_generation", 0)
                else state
            )
            db.execute(
                "UPDATE generic_improvement_jobs SET checkpoint_json=?,stage=? WHERE job_id=?",
                (json.dumps(cp, ensure_ascii=False, allow_nan=False), state, job_id),
            )
            db.execute(
                "UPDATE harness_jobs SET report_json=? WHERE job_id=?",
                (json.dumps(report, ensure_ascii=False, allow_nan=False), job_id),
            )
            db.execute("UPDATE jobs SET state=? WHERE id=?", (persisted_state, job_id))

    def _generic_case(self, package, partition, profile):
        if package["readiness"] != "replayable":
            raise EvaluationNotReady("initial task inputs unavailable")
        self._validate_package(package)
        spec = build_spec(package, builder=profile.spec_builder)
        # Production Wiki/recall may contain the answer to another validation
        # task. Keep the original capture for diagnosis/audit, but do not put it
        # in actor/proposer context without an experiment-aware read view.
        context = (
            profile.context_provider(deepcopy(package))
            if profile.context_provider
            else {}
        )
        if not isinstance(context, dict):
            raise TypeError(
                "experiment context provider must return an authorized view"
            )
        return {
            "id": package["id"],
            "lineage_id": package["lineage"],
            "partition": partition,
            "task": package["task"],
            "inputs": deepcopy(package["inputs"]),
            "editable_inputs": package["deliverable"].get("editable_inputs", []),
            "snapshot": {
                "evidence": context,
                "deliverable": package["deliverable"],
                "environment": package["environment"],
            },
            "tests": spec,
        }

    def _generic_session(self, cp, profile, root):
        protocol = {
            "driver": profile.version,
            "comparator": "generic-checks-v1",
            "experience_policy": cp["experience_policy"],
            "runtime": profile.contract(),
            "budget": asdict(profile.budget),
            "learning_policy": asdict(profile.policy),
            "train_rollouts": profile.train_rollouts,
        }
        session = FrozenEvaluationSession(
            cp["cases"],
            profile.driver_factory,
            evaluate_spec,
            profile.proposer,
            profile.policy,
            profile.budget,
            profile.train_rollouts,
            protocol,
            preparation_only=True,
        )
        # Appending independently selected tasks creates a new frozen manifest
        # generation. Previous execution records and manifests stay intact.
        generation = root / "generations" / session.manifest["sha256"]
        session.save(generation, resume=True)
        # Keep actual evaluation records in the existing observable layout.
        session._audit_root = root
        packages = {p["id"]: p for p in cp["packages"]}
        session.case_validator = lambda case: self._validate_package(
            packages[case["id"]]
        )
        cp["manifest"] = session.manifest
        manifests = cp.setdefault("manifest_generations", [])
        if session.manifest["sha256"] not in {m["sha256"] for m in manifests}:
            manifests.append(session.manifest)
        if self.recipe_memory_enabled:
            job_id = root.name
            with self._db() as db:
                job = db.execute(
                    "SELECT j.resource_id,h.principal FROM jobs j JOIN harness_jobs h ON h.job_id=j.id WHERE j.id=?",
                    (job_id,),
                ).fetchone()
            self.attach_recipe_memory(
                session, job_id, job["resource_id"], job["principal"]
            )
        return session

    def _process_generic_improvement(self, row):
        jid, principal = row["id"], row["principal"]
        with self._db() as db:
            item = db.execute(
                "SELECT checkpoint_json FROM generic_improvement_jobs WHERE job_id=?",
                (jid,),
            ).fetchone()
            db.execute(
                "UPDATE generic_improvement_jobs SET attempt=attempt+1 WHERE job_id=?",
                (jid,),
            )
            generation = db.execute(
                "SELECT generation FROM generic_evidence_generations WHERE principal=?",
                (principal,),
            ).fetchone()
        cp = json.loads(item[0])
        cp["principal"], cp["observed_generation"] = (
            principal,
            generation[0] if generation else 0,
        )
        run = self.get_run(row["resource_id"], principal)
        family = run["task_package"]["task_family"]
        if "initial" not in cp:
            with self._db() as db:
                recipe = db.execute(
                    "SELECT bundle_json FROM recipes WHERE id=?",
                    (row["base_recipe_id"],),
                ).fetchone()
            cp.update(
                initial=json.loads(recipe[0]) if recipe else seed_bundle(),
                base_recipe_id=row["base_recipe_id"],
                cases=[],
                packages=[],
                rounds=[],
                experience_policy=deepcopy(
                    run["snapshot"].get("experience_context", {})
                ),
            )
        source, packages = self.collect_task_packages(row["resource_id"], principal)
        cp["feedback_interpretations"] = [
            {
                "feedback_id": f["id"],
                "source_sha256": f["sha256"],
                "authority": f["authority"],
                "target_run_id": source["source_run_id"],
                "status": "explicit_acceptance"
                if f["body"].get("acceptance_checks")
                else "hypothesis_only",
            }
            for f in source["feedback"]
        ]
        if source["readiness"] != "replayable":
            self._generic_report(jid, "awaiting_input", cp, reason=source["gaps"])
            return
        profile = self.generic_runtime
        if profile is None or not profile.isolated:
            self._generic_report(
                jid,
                "awaiting_runner",
                cp,
                reason="configure one isolated global RuntimeProfile; no per-family backend required",
            )
            return
        contract_hash = fingerprint(profile.contract())
        if cp.get("runtime_sha256") not in (None, contract_hash):
            self._generic_report(
                jid,
                "awaiting_runner",
                cp,
                reason="frozen runtime changed; use a new experiment",
            )
            return
        cp["runtime_sha256"], cp["policy"] = contract_hash, asdict(profile.policy)
        if profile.policy.max_rounds != 1:
            self._generic_report(
                jid,
                "awaiting_runner",
                cp,
                reason="generic staged search v1 requires max_rounds=1; legacy multi-round learner remains supported",
            )
            return
        root = Path(self.db_path).resolve().parent / "harness-experiments" / jid
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        registry = BundleRegistry(root)
        # Reuse the existing policy validator and immutable Registry contracts.
        HierarchicalLearner(registry, profile.policy)
        registry.bootstrap(family, cp["initial"])
        if not cp["cases"]:
            try:
                case = self._generic_case(source, "train", profile)
            except EvaluationNotReady as exc:
                self._generic_report(jid, "awaiting_judge", cp, reason=str(exc))
                return
            self._validate_package(source)
            self._reserve_exposure([source], jid, "train", fingerprint(cp["initial"]))
            cp["packages"], cp["cases"] = [source], [case]
            self._generic_checkpoint(jid, cp, "train_reserved")
        for package in cp["packages"]:
            self._validate_package(package)
        session = self._generic_session(cp, profile, root)
        if "candidates" not in cp:
            # Commit a receipt before an external call. A process crash leaves
            # the job running for inspection, never eligible for blind replay.
            self._generic_report(jid, "running", cp)
            train = session.evaluate(
                cp["initial"], "train", session.partitions["train"]
            )
            if any(not r.get("judge_valid") for r in train):
                self._generic_report(
                    jid, "inconclusive", cp, reason="invalid train execution or judge"
                )
                return
            for record in train:
                record["expert_feedback"] = deepcopy(source["feedback"])
                # Raw observable events are training evidence, not a declaration
                # of complete action-trace telemetry.
                from .evidence import training_trajectory

                view = training_trajectory(source)
                record["captured_events"] = view.pop("events")
                record["trajectory_view"] = view
            diagnostic = diagnose(train, cp["initial"])
            proposal = session.propose(
                {
                    "instruction": PROPOSER_INSTRUCTION,
                    "bundle": deepcopy(cp["initial"]),
                    "train_evidence": train,
                    "archive": [],
                    "allowed_layer": None,
                    "diagnostic_context": diagnostic,
                    "max_candidates": profile.policy.max_candidates,
                }
            )
            cp["candidates"] = prepare_portfolio(
                cp["initial"],
                proposal,
                train,
                diagnostic,
                max_candidates=profile.policy.max_candidates,
            )
            cp["diagnostic"], cp["diagnosis"] = (
                diagnostic,
                proposal.get("diagnosis", {}),
            )
            for candidate in cp["candidates"]:
                registry.put(candidate["bundle"])
            if not cp["candidates"]:
                self._generic_report(
                    jid, "no_change", cp, reason="no evidenced method repair"
                )
                return
            self._generic_checkpoint(jid, cp, "candidates_ready")
        # Scope currently requires identical environment and family. The
        # namespace selects applicable evidence, never executable Python code.
        available = [
            p
            for p in packages
            if p["lineage"] != source["lineage"]
            and p["environment"] == source["environment"]
            and p["readiness"] == "replayable"
            and self._package_unexposed(p, jid)
        ]
        if "screen" not in cp:
            screen = self._select_generic_cases(available, "screen", 1, profile)
            if not screen:
                self._generic_report(
                    jid,
                    "awaiting_validation_tasks",
                    cp,
                    reason="one unexposed replayable screen task with valid criteria required",
                )
                return
            p, case = screen[0]
            self._validate_package(p)
            self._reserve_exposure([p], jid, "screen", fingerprint(cp["candidates"]))
            cp["packages"].append(p)
            cp["cases"].append(case)
            self._generic_checkpoint(jid, cp, "screen_reserved")
            session = self._generic_session(cp, profile, root)
            baseline = session.evaluate(
                cp["initial"], "screen", session.partitions["screen"]
            )
            for candidate in cp["candidates"]:
                records = session.evaluate(
                    candidate["bundle"], "screen", session.partitions["screen"]
                )
                candidate["screen"] = compare(
                    baseline,
                    records,
                    session.partitions["screen"],
                    base_sha=fingerprint(cp["initial"]),
                    candidate_sha=candidate["sha"],
                )
            cp["screen"] = True
            survivors = [
                c
                for c in cp["candidates"]
                if _screen_survives(c["screen"], profile.policy)
            ]
            if not survivors:
                state = (
                    "inconclusive"
                    if any(c["screen"]["coverage"] < 1 for c in cp["candidates"])
                    else "rejected"
                )
                self._generic_report(jid, state, cp, reason="no valid screen survivor")
                return
            winner = max(
                survivors,
                key=lambda c: (
                    c["screen"]["mean_delta"],
                    c["screen"]["exploration"]["metrics"][
                        profile.policy.exploration_metric
                    ]["mean_reduction"]
                    or 0,
                ),
            )
            cp["winner_sha"] = winner["sha"]
            self._generic_checkpoint(jid, cp, "winner_locked")
        winner = next(c for c in cp["candidates"] if c["sha"] == cp["winner_sha"])
        available = [p for p in available if self._package_unexposed(p, jid)]
        confirmation = self._select_generic_cases(
            available, "confirm", profile.policy.min_confirm_tasks, profile
        )
        if len(confirmation) < profile.policy.min_confirm_tasks:
            self._generic_report(
                jid,
                "awaiting_validation_tasks",
                cp,
                reason=f"{profile.policy.min_confirm_tasks} independent real confirmation tasks with valid criteria required",
            )
            return
        selected = [p for p, _ in confirmation]
        for p in selected:
            self._validate_package(p)
        self._reserve_exposure(selected, jid, "confirm", winner["sha"])
        cp["packages"].extend(selected)
        cp["cases"].extend(case for _, case in confirmation)
        registry.reserve_confirm(
            family, winner["sha"], [case["id"] for _, case in confirmation]
        )
        self._generic_checkpoint(jid, cp, "confirm_reserved")
        session = self._generic_session(cp, profile, root)
        before = session.evaluate(
            cp["initial"], "confirm", session.partitions["confirm"]
        )
        after = session.evaluate(
            winner["bundle"], "confirm", session.partitions["confirm"]
        )
        result = compare(
            before,
            after,
            session.partitions["confirm"],
            base_sha=fingerprint(cp["initial"]),
            candidate_sha=winner["sha"],
        )
        decision = confirm_decision(result, profile.policy, winner["sha"])
        cp["release_decision"] = decision
        cp["rounds"] = [
            {
                "family": family,
                "round": 1,
                "base_sha256": fingerprint(cp["initial"]),
                "diagnosis": cp["diagnosis"],
                "diagnostic_context": cp["diagnostic"],
                "decision": decision,
                "candidates": [
                    {k: v for k, v in c.items() if k != "bundle"}
                    for c in cp["candidates"]
                ],
            }
        ]
        cp["active"] = (
            winner["bundle"] if decision["status"] == "accepted" else cp["initial"]
        )
        state = (
            "validated"
            if decision["status"] == "accepted"
            else "inconclusive"
            if decision["status"] == "deferred"
            else "rejected"
        )
        self._generic_report(jid, state, cp, reason=decision["reason"])

    def _select_generic_cases(self, available, partition, count, profile):
        selected, keys = [], set()
        for package in available:
            if self._exposure_keys(package) & keys:
                continue
            try:
                case = self._generic_case(package, partition, profile)
            except EvaluationNotReady:
                continue
            selected.append((package, case))
            keys.update(self._exposure_keys(package))
            if len(selected) == count:
                break
        return selected

    def resume_improvement(self, job_id, principal):
        job = self.get_improvement(job_id, principal)
        from .task_packages import WAITING

        with self._db() as db:
            if not db.execute(
                "SELECT 1 FROM generic_improvement_jobs WHERE job_id=?", (job_id,)
            ).fetchone():
                raise ValueError("only generic preparation jobs support resume")
            if job["status"] not in WAITING:
                raise ValueError(
                    "only waiting jobs can resume; inspect running/unknown executions"
                )
            db.execute(
                "UPDATE jobs SET state='queued' WHERE id=? AND state=?",
                (job_id, job["status"]),
            )
        return {"job_id": job_id, "status": "queued"}
