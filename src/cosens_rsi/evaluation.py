"""Frozen local evaluation sessions for Recipe evolution.

Driver factories, test synthesis and comparators are operator-owned adapters,
never code supplied through MCP. Each arm gets a new directory and identical
public inputs/evidence; test specifications stay outside the actor prompt.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
import subprocess
import tempfile
from uuid import uuid4

from .algorithms.hierarchical import ExecutionBudget, HierarchicalExecutor, fingerprint
from .algorithms.hierarchical_learning import LearningPolicy
from .algorithms.trajectory import propose_repairs


def _safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("evaluation file path required")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("evaluation file escapes workspace")
    return path


def artifact_tests(workspace: Path, spec: dict) -> dict:
    """Small executable comparator for saved artifacts, suitable for local use."""
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("non-empty fixed artifact checks required")
    outcomes = []
    for check in checks:
        path = _safe_path(workspace, check["path"])
        kind = check["kind"]
        if kind not in {"exists", "contains", "json_equal"}:
            raise ValueError("unsupported artifact check")
        passed = path.is_file()
        if passed and kind == "contains":
            passed = check["value"] in path.read_text()
        elif passed and kind == "json_equal":
            try:
                passed = json.loads(path.read_text()) == check["value"]
            except (ValueError, UnicodeError):
                passed = False
        outcomes.append(passed)
    return {"judge_valid": True, "score": sum(outcomes) / len(outcomes),
            "task_success": all(outcomes), "checks": outcomes}


class CommandStepDriver:
    """JSON stdin/stdout adapter to an operator-configured agent executable.

    argv is fixed by server configuration, never taken from a Recipe. The
    executable performs the work in cwd and returns the StepDriver protocol.
    """
    def __init__(self, workspace: Path, argv: list[str]):
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("agent command must be a non-empty argv list")
        self.workspace, self.argv = Path(workspace), list(argv)

    def execute(self, instruction, *, timeout):
        result = subprocess.run(self.argv, input=json.dumps({"instruction": instruction}),
                                text=True, capture_output=True, cwd=self.workspace, timeout=timeout, check=False)
        if result.returncode:
            return {"completed": False, "error": "agent_command_failed", "outputs": {}}
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("agent command must return one JSON object")
        return value


class LocalHarnessBackend:
    """Configure a real StepDriver and independent comparator on the server.

    cases contain id, lineage_id, partition, task, inputs, snapshot and tests.
    Optional test_synthesizer receives only public task/inputs and runs once
    per case before any candidate execution. Returned tests are then frozen.
    """

    def __init__(self, cases: list[dict], driver_factory, *, driver_version: str,
                 comparator=artifact_tests, comparator_version="artifact-tests-v1",
                 proposer=propose_repairs, policy: LearningPolicy | None = None,
                 budget: ExecutionBudget | None = None, train_rollouts: int = 2,
                 test_synthesizer=None):
        if not driver_version or not comparator_version or type(train_rollouts) is not int or not 1 <= train_rollouts <= 8:
            raise ValueError("versioned adapters and 1..8 train rollouts required")
        self.cases = deepcopy(cases)
        self.driver_factory, self.comparator, self.proposer = driver_factory, comparator, proposer
        self.driver_version, self.comparator_version = driver_version, comparator_version
        self.policy, self.budget = policy or LearningPolicy(), budget or ExecutionBudget()
        self.train_rollouts, self.test_synthesizer = train_rollouts, test_synthesizer

    def open_session(self, source_run: dict):
        cases = deepcopy(self.cases)
        for case in cases:
            if self.test_synthesizer is not None:
                case["tests"] = self.test_synthesizer(deepcopy({"task": case["task"], "inputs": case.get("inputs", {})}))
        return FrozenEvaluationSession(cases, self.driver_factory, self.comparator, self.proposer,
            self.policy, self.budget, self.train_rollouts,
            {"driver": self.driver_version, "comparator": self.comparator_version,
             "experience_policy": source_run["snapshot"].get("experience_context", {}),
             "recipe_memory": source_run['snapshot'].get('recipe_memory',{}),
             "budget": asdict(self.budget), "learning_policy": asdict(self.policy),
             "train_rollouts": self.train_rollouts})


class FrozenEvaluationSession:
    def __init__(self, cases, driver_factory, comparator, proposer, policy, budget, train_rollouts, protocol,
                 *, preparation_only=False):
        # Canonical JSON breaks shared references and rejects non-finite data.
        self._cases = json.loads(json.dumps(cases, allow_nan=False))
        self._protocol = json.loads(json.dumps(protocol, allow_nan=False))
        self.driver_factory, self.comparator, self.proposer = driver_factory, comparator, proposer
        self.policy, self.budget, self.train_rollouts = policy, budget, train_rollouts
        self._audit_root = None
        self.case_validator = None
        self.evaluation_guard = None
        self.on_evaluation = None
        self._partitions = {p: [] for p in ("train", "screen", "confirm")}
        self._by_id = {}
        lineages, contents = {}, {}
        for case in self._cases:
            cid, partition = case["id"], case["partition"]
            if (not isinstance(cid, str) or not cid or cid in self._by_id or partition not in self._partitions
                    or not isinstance(case.get("task"), str) or not case["task"].strip()
                    or not isinstance(case.get("snapshot"), dict) or not case.get("tests")
                    or not isinstance(case.get("inputs", {}), dict)
                    or not isinstance(case.get("lineage_id"), str) or not case["lineage_id"]):
                raise ValueError("invalid evaluation case")
            content = fingerprint({"task": case["task"], "inputs": case.get("inputs", {})})
            for key, index in ((case["lineage_id"], lineages), (content, contents)):
                if key in index:
                    raise ValueError("evaluation cases duplicate task content or lineage")
                index[key] = cid
            if any(not isinstance(k, str) or not isinstance(v, str) for k, v in case.get("inputs", {}).items()):
                raise ValueError("local evaluation inputs must be text files")
            editable = case.get('editable_inputs', [])
            if not isinstance(editable, list) or any(not isinstance(name, str) or name not in case.get('inputs', {}) for name in editable):
                raise ValueError('editable inputs must name operator-owned case input files')
            self._by_id[cid] = case
            self._partitions[partition].append(cid)
        if not preparation_only and any(not ids for ids in self._partitions.values()):
            raise ValueError("train/screen/confirm must be non-empty")
        self._manifest_hash = fingerprint({"cases": self._cases, "protocol": self._protocol})

    @property
    def partitions(self):
        return deepcopy(self._partitions)

    @property
    def manifest(self):
        # No reference answers or hidden test bodies in public job reports.
        return {"sha256": self._manifest_hash, "protocol_sha256": fingerprint(self._protocol),
                "partitions": self.partitions,
                "cases": [{"id": c["id"], "lineage_id": c["lineage_id"],
                           "task_sha256": fingerprint({"task": c["task"], "inputs": c.get("inputs", {})}),
                           "snapshot_sha256": fingerprint(c["snapshot"]), "tests_sha256": fingerprint(c["tests"])}
                          for c in self._cases]}

    def propose(self, payload):
        # This adapter has no evaluation-case lookup in its payload. Evidence
        # supplied by the learner is train-only; archive data is aggregated.
        return self.proposer(deepcopy(payload))

    def save(self, root: Path, *, resume=False):
        """Keep replay inputs/tests privately; public reports contain hashes."""
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / "frozen-session.json"
        if resume and path.exists():
            saved = json.loads(path.read_text())
            if (saved.get("sha256") != self._manifest_hash or
                    fingerprint({"cases": saved["cases"], "protocol": saved["protocol"]}) != self._manifest_hash):
                raise ValueError("persisted evaluation session changed")
            self._audit_root = root
            return
        with path.open("x") as output:
            path.chmod(0o600)
            json.dump({"cases": self._cases, "protocol": self._protocol,
                       "sha256": self._manifest_hash}, output, ensure_ascii=False, allow_nan=False)
        self._audit_root = root

    def evaluate(self, bundle, partition, ids):
        if partition not in self._partitions or len(ids) != len(set(ids)) or not ids:
            raise ValueError("invalid evaluation partition")
        if any(cid not in self._partitions[partition] for cid in ids):
            raise ValueError("evaluation case outside partition")
        if fingerprint({"cases": self._cases, "protocol": self._protocol}) != self._manifest_hash:
            raise ValueError("evaluation session mutated")
        if self.evaluation_guard:
            self.evaluation_guard(partition, ids)
        records = []
        for cid in ids:
            case = self._by_id[cid]
            rollouts = []
            for _ in range(self.train_rollouts if partition == 'train' else 1):
                actual = self._execute(bundle, case)
                if self.on_evaluation:
                    self.on_evaluation(partition, deepcopy(actual))
                rollouts.append(actual)
            record = rollouts[0]
            if partition == "train":
                valid = all(r["judge_valid"] for r in rollouts)
                record = {**record, "rollouts": deepcopy(rollouts), "judge_valid": valid,
                          "score": mean(r["score"] for r in rollouts) if valid else None,
                          "task_success": all(r["task_success"] for r in rollouts)}
            records.append(record)
        return records

    def _execute(self, bundle, case):
        if self.case_validator:
            self.case_validator(case)
        with tempfile.TemporaryDirectory(prefix="rsi-eval-") as directory:
            root = Path(directory).resolve()
            for name, content in case.get("inputs", {}).items():
                path = _safe_path(root, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            driver = self.driver_factory(root)
            context = {"snapshot": deepcopy(case["snapshot"]),
                       "experience_policy": deepcopy(self._protocol["experience_policy"]),
                       "recipe_memory": deepcopy(self._protocol.get('recipe_memory',{}))}
            result = HierarchicalExecutor().run(deepcopy(bundle), case["task"], root, driver,
                                                self.budget, evidence_context=context,
                                                editable_inputs=case.get('editable_inputs', []))
            if result["status"] == "completed":
                try:
                    judgement = self.comparator(root, deepcopy(case["tests"]))
                except Exception:
                    judgement = {"judge_valid": False, "score": None, "task_success": False}
            else:
                judgement = {"judge_valid": result["status"] == "failed", "score": 0, "task_success": False}
            criterion_manifest = [c["key"] for c in case["tests"].get("checks", [])] if case["tests"].get("schema") == "generic-evaluation-spec-v1" else None
            if criterion_manifest is not None:
                # A failed actor is still evaluated against the whole frozen
                # specification; an adapter/provider failure stays invalid.
                if result["status"] == "failed":
                    try:
                        judgement = self.comparator(root, deepcopy(case["tests"]))
                        judgement["task_success"] = False
                        judgement["critical_failure"] = True
                    except Exception:
                        judgement = {"judge_valid": False, "score": None, "task_success": False}
                actual = judgement.get("quality_checks", [])
                if (len(actual) != len(criterion_manifest) or
                        {c.get("key") for c in actual} != set(criterion_manifest)):
                    judgement["judge_valid"] = False
            score = judgement.get("score")
            valid = (judgement.get("judge_valid") is True and type(score) in (int, float)
                     and math.isfinite(score) and 0 <= score <= 1 and type(judgement.get("task_success")) is bool)
            record = {"evaluation_id": "eval_" + uuid4().hex,
                    "task_id": case["id"], "task_lineage_id": case["lineage_id"],
                    "task_sha256": fingerprint({"task": case["task"], "inputs": case.get("inputs", {})}),
                    "bundle_sha256": result["bundle_sha256"], "protocol_sha256": fingerprint(self._protocol),
                    "snapshot_sha256": fingerprint(case["snapshot"]), "tests_sha256": fingerprint(case["tests"]),
                    "experience_policy_sha256": fingerprint(self._protocol["experience_policy"]),
                    "session_sha256": self._manifest_hash, "score": score if valid else None,
                    "judge_valid": valid, "task_success": valid and judgement.get("task_success") is True,
                    "elapsed_sec": result["elapsed_sec"], "action_trace": result["action_trace"],
                    "execution_status": result["status"], "trace": result["trace"],
                    "task_prompt": case["task"], "feedback": str(judgement.get("feedback", "fixed artifact tests evaluated"))[:6000],
                    "quality_checks": judgement.get("quality_checks", [])}
            if criterion_manifest is not None:
                record["criterion_manifest"] = criterion_manifest
                record["critical_failure"] = judgement.get("critical_failure", True)
            if self._audit_root is not None:
                audit = self._audit_root / "runs" / record["evaluation_id"]
                audit.mkdir(parents=True, mode=0o700)
                artifacts = []
                for name in result["final"] if isinstance(result["final"], list) else []:
                    source = _safe_path(root, name)
                    if not source.is_file():
                        continue
                    data = source.read_bytes()
                    target = _safe_path(audit / "artifacts", name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o600)
                    # The file hash is over bytes, independent of JSON encoding.
                    artifacts.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
                record["artifacts"] = artifacts
                log_names = {name for step in result["trace"] for name in step.get("audit_files", [])}
                logs = []
                for name in sorted(log_names):
                    source = _safe_path(root, name)
                    if not source.is_file():
                        continue
                    data = source.read_bytes()
                    target = _safe_path(audit / "telemetry", name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    target.chmod(0o600)
                    logs.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
                if logs:
                    record["raw_logs"] = logs
                audit_file = audit / "record.json"
                with audit_file.open("x") as output:
                    audit_file.chmod(0o600)
                    json.dump({"record": record, "judgement": judgement}, output, ensure_ascii=False, allow_nan=False)
            return record
