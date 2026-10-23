"""Quality and exploration search over immutable hierarchical Recipes.

The proposer receives train records and bounded archive aggregates. Screen
selects a candidate; independent confirm authorizes experimental promotion.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from statistics import mean
from typing import Callable

from .hierarchical import fingerprint, freeze_bundle, mutate
from .trajectory import diagnose, measure_trace


class BundleRegistry:
    """Content-addressed bundles, version collision checks and atomic activation."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "hierarchical.sqlite3"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bundles (sha TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (ref TEXT PRIMARY KEY, sha TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS active (family TEXT PRIMARY KEY, sha TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY, data TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS confirm_reservations (
                    family TEXT NOT NULL, task_id TEXT NOT NULL, candidate TEXT NOT NULL,
                    PRIMARY KEY(family, task_id));
                CREATE TABLE IF NOT EXISTS activations (
                    id INTEGER PRIMARY KEY, family TEXT NOT NULL, sha TEXT NOT NULL,
                    previous_sha TEXT, kind TEXT NOT NULL);
            """)

    def put(self, bundle: dict) -> str:
        frozen = freeze_bundle(bundle)
        data, sha = frozen["bundle"], frozen["sha256"]
        versions = {f"step:{ref}": value for ref, value in frozen["step_sha256"].items()}
        flow = data["flow"]
        versions[f"flow:{flow['id']}@{flow['version']}"] = frozen["flow_sha256"]
        with sqlite3.connect(self.db) as db:
            for ref, digest in versions.items():
                prior = db.execute("SELECT sha FROM versions WHERE ref=?", (ref,)).fetchone()
                if prior and prior[0] != digest:
                    raise ValueError("an immutable Recipe version was redefined")
                db.execute("INSERT OR IGNORE INTO versions VALUES (?, ?)", (ref, digest))
            db.execute("INSERT OR IGNORE INTO bundles VALUES (?, ?)", (sha, json.dumps(data, ensure_ascii=False)))
        return sha

    def get(self, sha: str) -> dict:
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT data FROM bundles WHERE sha=?", (sha,)).fetchone()
        if row is None:
            raise ValueError("unknown bundle")
        result = json.loads(row[0])
        if freeze_bundle(result)["sha256"] != sha:
            raise ValueError("bundle integrity mismatch")
        return result

    def active(self, family: str) -> dict | None:
        with sqlite3.connect(self.db) as db:
            row = db.execute("SELECT sha FROM active WHERE family=?", (family,)).fetchone()
        return self.get(row[0]) if row else None

    def bootstrap(self, family: str, bundle: dict) -> None:
        sha = self.put(bundle)
        with sqlite3.connect(self.db) as db:
            cursor = db.execute("INSERT OR IGNORE INTO active VALUES (?, ?)", (family, sha))
            if cursor.rowcount:
                db.execute("INSERT INTO activations(family,sha,previous_sha,kind) VALUES (?, ?, NULL, 'bootstrap')", (family, sha))

    def record(self, decision: dict) -> None:
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO decisions(data) VALUES (?)", (json.dumps(decision, ensure_ascii=False),))

    def archive(self, family: str | None = None) -> list[dict]:
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT data FROM decisions ORDER BY id").fetchall()
        result = []
        for row in rows:
            record = json.loads(row[0])
            if family is not None and record.get("family") != family:
                continue
            for candidate in record.get("candidates", []):
                change, screen = candidate["change"], candidate["screen"]
                if screen["coverage"] < 1:
                    continue
                result.append({"base_sha256": record["base_sha256"], "change_sha256": fingerprint(change),
                               "layer": change["layer"], "target": change.get("target", change.get("node")),
                               "mean_delta": screen["mean_delta"], "regressions": screen["regressed"]})
        return result

    def used_confirm(self, family: str) -> set[str]:
        with sqlite3.connect(self.db) as db:
            return {r[0] for r in db.execute("SELECT task_id FROM confirm_reservations WHERE family=?", (family,))}

    def reserve_confirm(self, family: str, candidate: str, task_ids: list[str]) -> None:
        # Reserve before evaluation: a crash cannot make exposed confirm
        # tasks eligible for selecting another hypothesis in a later run.
        with sqlite3.connect(self.db) as db:
            try:
                db.executemany("INSERT INTO confirm_reservations VALUES (?, ?, ?)",
                               [(family, task, candidate) for task in task_ids])
            except sqlite3.IntegrityError:
                raise ValueError("confirm tasks have already been exposed") from None

    def promote(self, family: str, base: str, candidate: str, decision: dict) -> None:
        if decision.get("status") != "accepted" or decision.get("candidate_sha256") != candidate:
            raise ValueError("promotion requires candidate-specific confirm evidence")
        self.get(candidate)
        with sqlite3.connect(self.db) as db:
            cursor = db.execute("UPDATE active SET sha=? WHERE family=? AND sha=?", (candidate, family, base))
            if cursor.rowcount != 1:
                raise ValueError("active base changed during search")
            db.execute("INSERT INTO activations(family,sha,previous_sha,kind) VALUES (?, ?, ?, 'promote')", (family, candidate, base))
            db.execute("INSERT INTO decisions(data) VALUES (?)", (json.dumps(decision, ensure_ascii=False),))

    def rollback(self, family: str, reason: str) -> dict:
        """Restore the entire prior promoted bundle, preserving every version."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("rollback requires a reason")
        with sqlite3.connect(self.db) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT sha FROM active WHERE family=?", (family,)).fetchone()
            prior = db.execute("SELECT previous_sha FROM activations WHERE family=? AND sha=? "
                               "AND kind='promote' ORDER BY id DESC LIMIT 1", (family, active[0] if active else "")).fetchone()
            if not active or not prior or not prior[0]:
                raise ValueError("no prior promotion to roll back")
            db.execute("UPDATE active SET sha=? WHERE family=?", (prior[0], family))
            db.execute("INSERT INTO activations(family,sha,previous_sha,kind) VALUES (?, ?, ?, 'rollback')", (family, prior[0], active[0]))
            db.execute("INSERT INTO decisions(data) VALUES (?)", (json.dumps({"status": "rolled_back", "family": family,
                "from": active[0], "to": prior[0], "reason": reason}, ensure_ascii=False),))
        return self.get(prior[0])


@dataclass(frozen=True)
class LearningPolicy:
    max_rounds: int = 3
    max_candidates: int = 4
    min_confirm_tasks: int = 4
    min_delta: float = 0.03
    max_latency_ratio: float | None = None
    exploration_enabled: bool = True
    noninferiority_margin: float = 0.01
    min_exploration_reduction: float = 0.1
    exploration_metric: str = "actions"


def _interval(values: list[float]) -> list:
    if not values:
        return [None, None]
    rng = random.Random(20261022)
    samples = sorted(mean(rng.choices(values, k=len(values))) for _ in range(4000))
    return [samples[100], samples[3899]]


def _exploration_pair(before: dict, after: dict) -> dict | None:
    # Missing/partial action telemetry is not an efficiency measurement. Only
    # independently successful pairs qualify; failed early exits cannot win.
    if before.get("task_success") is not True or after.get("task_success") is not True:
        return None
    try:
        a, b = (measure_trace(r.get("action_trace")) for r in (before, after))
    except ValueError:
        return None
    return {k: {"base": a[k], "candidate": b[k],
                "reduction": (a[k] - b[k]) / a[k] if a[k] else 0}
            for k in ("actions", "failed_attempts", "repeated_actions", "repairs")}


def compare(base: list[dict], candidate: list[dict], expected: list[str],
            *, base_sha: str, candidate_sha: str) -> dict:
    """Only complete, judge-valid, frozen task pairs contribute to quality."""
    if len(expected) != len(set(expected)) or not expected:
        raise ValueError("comparison manifest needs unique tasks")
    by_arm = []
    for records, sha in ((base, base_sha), (candidate, candidate_sha)):
        index = {r["task_id"]: r for r in records}
        if len(index) != len(records) or set(index) != set(expected):
            raise ValueError("comparison does not cover the exact manifest")
        if any(r.get("bundle_sha256") != sha for r in records):
            raise ValueError("evaluation bundle mismatch")
        by_arm.append(index)
    pairs = []
    invalid = []
    for task in expected:
        before, after = by_arm[0][task], by_arm[1][task]
        if (not before.get("task_sha256") or not before.get("protocol_sha256")
                or before.get("task_sha256") != after.get("task_sha256")
                or before.get("protocol_sha256") != after.get("protocol_sha256")):
            raise ValueError("task or execution/evaluation protocol changed")
        for field in ("snapshot_sha256", "tests_sha256", "experience_policy_sha256", "session_sha256"):
            if (field in before or field in after) and (not before.get(field) or before.get(field) != after.get(field)):
                raise ValueError(f"frozen {field} changed")
        scores = (before.get("score"), after.get("score"))
        expected_checks = before.get("criterion_manifest")
        if expected_checks is not None or "criterion_manifest" in after:
            if expected_checks != after.get("criterion_manifest"):
                raise ValueError("frozen criterion manifest changed")
            if any(len(r.get("quality_checks", [])) != len(expected_checks or []) or
                   {c.get("key") for c in r.get("quality_checks", [])} != set(expected_checks or [])
                   for r in (before, after)):
                invalid.append(task)
                continue
        if not all(r.get("judge_valid") and isinstance(r.get("score"), (int, float))
                   and not isinstance(r["score"], bool) and 0 <= r["score"] <= 1
                   for r in (before, after)):
            invalid.append(task)
            continue
        before_checks = {c["key"]: c for c in before.get("quality_checks", []) if isinstance(c, dict) and isinstance(c.get("key"), str)}
        after_checks = {c["key"]: c for c in after.get("quality_checks", []) if isinstance(c, dict) and isinstance(c.get("key"), str)}
        critical_check_regression = any(c.get("critical") is True and c.get("passed") is True
            and after_checks.get(key, {}).get("passed") is not True for key, c in before_checks.items())
        pairs.append({"task_id": task, "base": scores[0], "candidate": scores[1],
                      "base_evaluation_id": before.get("evaluation_id"),
                      "candidate_evaluation_id": after.get("evaluation_id"),
                      "delta": scores[1] - scores[0],
                      "critical_regression": scores[0] >= 0.6 > scores[1] or critical_check_regression
                          or after.get("critical_failure") is True,
                      "success_regression": before.get("task_success") is True and after.get("task_success") is not True,
                      "exploration": _exploration_pair(before, after),
                      "latency_ratio": after.get("elapsed_sec", 0) / max(before.get("elapsed_sec", 0), .001)})
    deltas = [p["delta"] for p in pairs]
    ci = _interval(deltas)
    measured = [p for p in pairs if p["exploration"] is not None]
    exploration = {"coverage": len(measured) / len(expected), "valid_tasks": len(measured), "metrics": {}}
    for metric in ("actions", "failed_attempts", "repeated_actions", "repairs"):
        reductions = [p["exploration"][metric]["reduction"] for p in measured]
        exploration["metrics"][metric] = {"mean_reduction": mean(reductions) if reductions else None,
                                            "bootstrap_95_ci": _interval(reductions)}
    return {"coverage": len(pairs) / len(expected), "valid_tasks": len(pairs), "invalid_tasks": invalid,
            "mean_delta": mean(deltas) if deltas else None, "bootstrap_95_ci": ci,
            "improved": sum(d > 0 for d in deltas), "regressed": sum(d < 0 for d in deltas),
            "tied": sum(d == 0 for d in deltas), "critical_regressions": sum(p["critical_regression"] for p in pairs),
            "success_regressions": sum(p["success_regression"] for p in pairs), "exploration": exploration,
            "max_latency_ratio": max((p["latency_ratio"] for p in pairs), default=None), "pairs": pairs}


def confirm_decision(comparison: dict, policy: LearningPolicy, candidate_sha: str) -> dict:
    path = None
    if comparison["coverage"] < 1:
        status, reason = "deferred", "incomplete judge-valid coverage"
    elif comparison["critical_regressions"] or comparison.get("success_regressions", 0):
        status, reason = "rejected", "critical task regression"
    elif comparison["valid_tasks"] < policy.min_confirm_tasks:
        status, reason = "deferred", "too few independent confirm tasks"
    elif policy.max_latency_ratio is not None and comparison["max_latency_ratio"] > policy.max_latency_ratio:
        status, reason = "rejected", "execution cost/latency budget exceeded"
    elif comparison["mean_delta"] >= policy.min_delta and comparison["bootstrap_95_ci"][0] > 0:
        status, reason, path = "accepted", "independent confirm gate passed", "quality"
    elif policy.exploration_enabled and comparison["bootstrap_95_ci"][0] > -policy.noninferiority_margin:
        exploration = comparison.get("exploration", {})
        metric = exploration.get("metrics", {}).get(policy.exploration_metric, {})
        if exploration.get("coverage") != 1:
            status, reason = "deferred", "complete successful action traces required for exploration confirmation"
        elif (metric["mean_reduction"] >= policy.min_exploration_reduction
              and metric["bootstrap_95_ci"][0] > 0):
            status, reason, path = "accepted", "quality noninferiority and exploration reduction confirmed", "exploration"
        else:
            status, reason = "rejected", "exploration reduction below preregistered minimum"
    elif comparison["mean_delta"] < policy.min_delta:
        status, reason = "rejected", "effect below preregistered minimum"
    else:
        status, reason = "deferred", "uncertain transfer effect"
    return {"status": status, "reason": reason, "acceptance_path": path,
            "candidate_sha256": candidate_sha, "comparison": comparison}


def _screen_survives(result: dict, policy: LearningPolicy) -> bool:
    if result["coverage"] != 1 or result["critical_regressions"] or result.get("success_regressions", 0):
        return False
    if result["mean_delta"] > 0 and result["improved"] > result["regressed"]:
        return True
    exploration = result.get("exploration", {})
    metric = exploration.get("metrics", {}).get(policy.exploration_metric, {})
    return (policy.exploration_enabled and result["mean_delta"] > -policy.noninferiority_margin
            and exploration.get("coverage") == 1
            and metric.get("mean_reduction", 0) >= policy.min_exploration_reduction)


def _check_proposal(change: dict, train: list[dict]) -> None:
    # Task IDs and copied answer passages are rejected; learned domain
    # procedures are permitted. This is a guard, not a proof of semantic privacy.
    text = json.dumps(change, ensure_ascii=False).lower()
    if re.search(r"(?:sk-|tr_)[a-z0-9_]{16,}", text):
        raise ValueError("credential-like content in proposed Recipe")
    for record in train:
        task_id = str(record.get("task_id", ""))
        if task_id and re.search(r"(?<![\w-])" + re.escape(task_id.lower()) + r"(?![\w-])", text):
            raise ValueError("training case identifier in proposed Recipe")
        # Only task/answer text is checked, not existing method instructions.
        for source in (record.get("task_prompt", ""), record.get("answer_text", "")):
            words = str(source).lower().split()
            if any(" ".join(words[i:i + 14]) in text for i in range(max(0, len(words) - 13))):
                raise ValueError("copied training passage in proposed Recipe")


def prepare_portfolio(bundle, proposal, train, diagnostic, *, max_candidates=4,
                      allowed_layer=None, archive=()):
    """Shared local-search validation for full and resumable experiments."""
    changes = proposal.get("changes", [])
    if changes == [] and proposal.get("diagnosis", {}).get("layer") == "none":
        return []
    if not 2 <= len(changes) <= max_candidates:
        raise ValueError("proposer must supply a 2..N portfolio")
    mechanism = proposal.get("diagnosis", {}).get("mechanism_id")
    cards = [c for c in diagnostic["cards"] if c["mechanism_id"] == mechanism]
    if mechanism and not cards:
        raise ValueError("proposal cites an unsupported failure mechanism")
    if cards and any((c.get("layer") == "step" and c.get("target") not in {d["binding"] for d in cards})
                     or (c.get("layer") == "flow" and c.get("node") not in {d["node"] for d in cards}) for c in changes):
        raise ValueError("repairs must target the diagnosed mechanism")
    keys = {(c.get("layer"), c.get("target", c.get("node")), c.get("slot", c.get("op"))) for c in changes}
    if ((len(keys) != 1 and not cards) or
            (allowed_layer and any(c.get("layer") != allowed_layer for c in changes))):
        raise ValueError("portfolio must change one layer and one target")
    prepared = []
    for change in changes:
        _check_proposal(change, train)
        if ((cards and any(c.get("evidence_kind") == "artifact_feedback" for c in cards))
                or (change.get("layer") == "flow" and change.get("op") == "remove")):
            if change.get("layer") == "flow" and not any(c.get("kind") == "duplicate_node" for c in cards):
                raise ValueError("flow pruning requires complete action telemetry")
        candidate = mutate(bundle, change)
        sha = fingerprint(candidate)
        if (sha in {c["sha"] for c in prepared} or any(a["change_sha256"] == fingerprint(change)
                and a.get("base_sha256") == fingerprint(bundle) for a in archive)):
            raise ValueError("duplicate or already tested proposal")
        prepared.append({"sha": sha, "change": change, "bundle": candidate})
    return prepared


PROPOSER_INSTRUCTION = """You optimize a hierarchical Recipe from TRAIN evidence.
Both the optimizer and its instructions are domain-neutral. You MAY learn
specialized reusable procedures when supported by train evidence. Never copy
case answers, task identifiers, names, filenames, quotations, secrets or case
numeric facts into a Recipe. Do not edit agent, tools, evaluator, budgets or I/O
contracts. Diagnose one falsifiable failure or exploration mechanism and
propose 2..4 genuinely distinct minimal repairs of that mechanism.
When diagnostic_context contains a supported mechanism_id, alternatives may
repair that ONE mechanism through different step slots or graph edits. Each
candidate remains one minimal edit. Otherwise use the same target and slot.
Honor allowed_layer when it is provided and return no more than max_candidates.
Use archive outcomes to avoid repeating failed proposals. Valid changes:
step: {layer:'step', target:<binding name>, slot:<preconditions|steps|checks|recovery>, value:[instructions]}
flow: {layer:'flow', op:'rewire', node:<id>, value:{inputs:{port:source},after:[ids]}}
or flow op 'bind' with value an existing skill binding. 'insert' is one splice:
value:{node:<complete node using existing binding>,consumer:<existing node>,
input_port:<consumer input>,output_port:<new node output>}; it inserts the node
and redirects that consumer input. 'remove' has value:{redirects:{removed_output:
replacement_source}} to reconnect all consumers and preserve ordering dependencies.
Every flow change must leave a valid DAG and all nodes must contribute to final.
Return JSON {diagnosis:{layer,reason,expected_effect,counterexample},changes:[...]}
only. A low final score alone is insufficient diagnosis; ground the hypothesis
in train feedback or execution trace. Never request or use screen/confirm tasks.
Also diagnose successful runs with repeated no-progress actions or avoidable
repairs. Preserve required checks, source inputs and acceptance standards.
Never infer redundant work from truncated UI projections or unavailable logs.
Include diagnosis.mechanism_id from diagnostic_context for such repairs.
If no method failure or exploration defect is supported, return diagnosis:{layer:'none',reason:<why>}
and changes:[] instead of inventing an intervention.
"""


class HierarchicalLearner:
    """Evidence-directed coordinate search, with a separate confirmation stage."""

    def __init__(self, registry: BundleRegistry, policy: LearningPolicy | None = None):
        self.registry = registry
        self.policy = policy or LearningPolicy()
        if not 1 <= self.policy.max_rounds <= 3 or not 2 <= self.policy.max_candidates <= 4:
            raise ValueError("learning budget supports 1..3 rounds, 2..4 candidates")
        if (type(self.policy.min_confirm_tasks) is not int or self.policy.min_confirm_tasks < 2
                or not math.isfinite(self.policy.min_delta) or self.policy.min_delta < 0
                or (self.policy.max_latency_ratio is not None and
                    (not math.isfinite(self.policy.max_latency_ratio) or self.policy.max_latency_ratio <= 0))
                or type(self.policy.exploration_enabled) is not bool
                or not 0 < self.policy.noninferiority_margin <= 0.05
                or not 0 < self.policy.min_exploration_reduction <= 1
                or self.policy.exploration_metric not in {"actions", "failed_attempts", "repeated_actions", "repairs"}):
            raise ValueError("invalid confirm policy")

    def run(self, family: str, initial: dict, partitions: dict[str, list[str]],
            propose: Callable[[dict], dict], evaluate: Callable[[dict, str, list[str]], list[dict]],
            *, allowed_layer: str | None = None) -> dict:
        if set(partitions) != {"train", "screen", "confirm"} or any(not v or len(v) != len(set(v)) for v in partitions.values()):
            raise ValueError("freeze non-empty unique train/screen/confirm manifests")
        sets = [set(v) for v in partitions.values()]
        if any(sets[i] & sets[j] for i in range(3) for j in range(i)):
            raise ValueError("learning partitions overlap")
        if allowed_layer not in (None, "step", "flow"):
            raise ValueError("invalid layer ablation")
        self.registry.bootstrap(family, initial)
        active = self.registry.active(family)
        archive = self.registry.archive(family)
        rounds = []
        confirm_cursor = 0
        confirm_pool = [task for task in partitions["confirm"] if task not in self.registry.used_confirm(family)]
        for round_index in range(self.policy.max_rounds):
            base_sha = self.registry.put(active)
            train = evaluate(active, "train", partitions["train"])
            if any(not r.get("judge_valid") for r in train):
                return {"active": active, "rounds": rounds, "status": "blocked", "reason": "invalid train evaluation"}
            diagnostic = diagnose(train, active)
            proposal = propose({"instruction": PROPOSER_INSTRUCTION, "bundle": active,
                                "train_evidence": train, "archive": archive, "allowed_layer": allowed_layer,
                                "diagnostic_context": diagnostic,
                                "max_candidates": self.policy.max_candidates})
            changes = proposal.get("changes", [])
            if changes == [] and proposal.get("diagnosis", {}).get("layer") == "none":
                record = {"family": family, "round": round_index + 1, "base_sha256": base_sha,
                          "diagnosis": proposal["diagnosis"], "candidates": [],
                          "decision": {"status": "stopped", "reason": "no evidenced method failure"}}
                self.registry.record(record)
                rounds.append(record)
                return {"active": active, "rounds": rounds, "status": "completed",
                        "termination_reason": "no_learnable_failure"}
            prepared = prepare_portfolio(active, proposal, train, diagnostic,
                max_candidates=self.policy.max_candidates, allowed_layer=allowed_layer, archive=archive)
            for candidate in prepared:
                self.registry.put(candidate["bundle"])
            base_screen = evaluate(active, "screen", partitions["screen"])
            candidates = []
            for prepared_candidate in prepared:
                change, candidate, sha = (prepared_candidate[k] for k in ("change", "bundle", "sha"))
                evidence = evaluate(candidate, "screen", partitions["screen"])
                score = compare(base_screen, evidence, partitions["screen"], base_sha=base_sha, candidate_sha=sha)
                candidates.append({"sha": sha, "change": change, "screen": score, "bundle": candidate})
            survivors = [c for c in candidates if _screen_survives(c["screen"], self.policy)]
            decision = {"status": "rejected", "reason": "no screen survivor"}
            if any(c["screen"]["coverage"] < 1 for c in candidates):
                decision = {"status": "deferred", "reason": "incomplete screen judge coverage; no methodology verdict"}
            elif survivors:
                # Quality first; exploration breaks ties. No monetary objective.
                winner = max(survivors, key=lambda c: (c["screen"]["mean_delta"],
                    c["screen"]["exploration"]["metrics"][self.policy.exploration_metric]["mean_reduction"] or 0))
                fresh_confirm = confirm_pool[confirm_cursor:confirm_cursor + self.policy.min_confirm_tasks]
                confirm_cursor += len(fresh_confirm)
                if len(fresh_confirm) < self.policy.min_confirm_tasks:
                    record = {"family": family, "round": round_index + 1, "base_sha256": base_sha,
                              "decision": {"status": "deferred", "reason": "too few independent confirm tasks"}}
                    self.registry.record(record)
                    rounds.append(record)
                    break
                self.registry.reserve_confirm(family, winner["sha"], fresh_confirm)
                before = evaluate(active, "confirm", fresh_confirm)
                after = evaluate(winner["bundle"], "confirm", fresh_confirm)
                result = compare(before, after, fresh_confirm, base_sha=base_sha, candidate_sha=winner["sha"])
                decision = confirm_decision(result, self.policy, winner["sha"])
                if decision["status"] == "accepted":
                    self.registry.promote(family, base_sha, winner["sha"], decision)
                    active = winner["bundle"]
            record = {"family": family, "round": round_index + 1, "base_sha256": base_sha,
                      "diagnosis": proposal.get("diagnosis"), "diagnostic_context": diagnostic, "decision": decision,
                      "candidates": [{k: v for k, v in c.items() if k != "bundle"} for c in candidates]}
            self.registry.record(record)
            rounds.append(record)
            archive.extend({"base_sha256": base_sha, "change_sha256": fingerprint(c["change"]), "layer": c["change"]["layer"],
                            "target": c["change"].get("target", c["change"].get("node")),
                            "mean_delta": c["screen"]["mean_delta"], "regressions": c["screen"]["regressed"]}
                           for c in candidates if c["screen"]["coverage"] == 1)
            if decision["status"] == "deferred":
                break
            if len(rounds) >= 2 and all(r["decision"]["status"] != "accepted" for r in rounds[-2:]):
                break
        return {"active": active, "rounds": rounds, "status": "completed"}
