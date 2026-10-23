"""One operator-owned Agent runtime for all task families.

An executable boundary is configured once per deployment. A workspace path or
prompt is not a sandbox: generic validation requires an operator-attested
isolated runner. No API accepts commands, credentials or isolation assertions.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from .algorithms.hierarchical import ExecutionBudget, fingerprint
from .algorithms.hierarchical_learning import LearningPolicy
from .algorithms.trajectory import propose_repairs


def run_codex(workspace, prompt, timeout, *, model="gpt-6-luna", reasoning="medium"):
    """Shared launcher, also used by the existing refund integration.

    The local Codex launcher by itself does not attest isolation. Deployments
    can wrap it in a dedicated runner/container behind RuntimeProfile.
    """
    workspace = Path(workspace)
    command = [
        str(Path.home() / ".npm-global/bin/codex"),
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-C",
        str(workspace),
        "--json",
        "-m",
        model,
        "-c",
        "model_reasoning_effort=" + json.dumps(reasoning),
        "-c",
        "apps._default.enabled=false",
        "-c",
        "mcp_servers.rsi.enabled=false",
        "-c",
        "mcp_servers.cosens_rsi.enabled=false",
        "-",
    ]
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COSENS_", "MEMORY_", "FRIDAY_MEMORY_"))
    }
    result = subprocess.run(
        command,
        input=prompt,
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    (workspace / "observable.jsonl").write_text(result.stdout)
    (workspace / "provider.stderr.log").write_text(result.stderr)
    return result


class GenericAgentStepDriver:
    """Existing JSON StepDriver protocol with a deployment-wide command."""

    def __init__(self, workspace, argv):
        self.workspace, self.argv = Path(workspace), list(argv)

    def execute(self, instruction, *, timeout):
        # Provider credentials can be provisioned inside the runner. Service
        # and Memory/admin credentials never enter the actor's environment.
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("COSENS_", "MEMORY_", "FRIDAY_MEMORY_"))
        }
        result = subprocess.run(
            self.argv,
            input=json.dumps({"instruction": instruction}),
            text=True,
            capture_output=True,
            cwd=self.workspace,
            env=env,
            timeout=timeout,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("isolated agent runner failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise TypeError("agent runner must return one StepDriver object")
        return value


class CodexStepDriver:
    """Reuse the existing Codex launcher without business filenames or logic.

    Hosting this driver requires a real isolated worker boundary, just as for
    CommandStepDriver. Native logs are retained as partial observable evidence.
    """

    def __init__(self, workspace, *, launch=run_codex):
        self.workspace, self.launch = Path(workspace), launch

    def execute(self, instruction, *, timeout):
        identifier = uuid4().hex
        response_name = ".cosens-node-" + identifier + ".json"
        prompt = (
            "Execute the frozen workflow node below using only its public task and this workspace. "
            "Save the actual requested deliverables. Also save "
            + response_name
            + ' containing exactly {"outputs":{...}} matching the node\'s output_contract. '
            "For text ports use text; for artifact ports use saved relative file paths. "
            "Do not include hidden acceptance tests, claim external approval, or invent action telemetry.\n"
            + instruction
        )
        result = self.launch(self.workspace, prompt, timeout)
        logs = []
        for name in ("observable.jsonl", "provider.stderr.log"):
            source = self.workspace / name
            if source.is_file():
                target = self.workspace / (".cosens-" + identifier + "-" + name)
                source.rename(target)
                logs.append(target.name)
        if result.returncode:
            raise RuntimeError("Codex runner failed")
        path = self.workspace / response_name
        if not path.is_file():
            return {
                "completed": True,
                "outputs": {},
                "error": "node_output_manifest_missing",
                "audit_files": logs,
            }
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or not isinstance(
            payload.get("outputs"), dict
        ):
            raise TypeError("invalid node output manifest")
        return {
            "completed": True,
            "outputs": payload["outputs"],
            "audit_files": logs,
            "response": "Output manifest saved; native logs are partial action evidence.",
        }


class CodexJSONRole:
    """Stateless proposer/compiler/reviewer using the same deployed launcher."""

    def __init__(self, version, *, launch=run_codex, timeout=600):
        self.version, self.launch, self.timeout = version, launch, timeout

    def __call__(self, payload):
        with tempfile.TemporaryDirectory(prefix="cosens-optimizer-") as directory:
            workspace = Path(directory)
            prompt = (
                "Use only the supplied evidence. Save response.json containing the requested JSON object. "
                "Do not read external files, call Memory/MCP, or seek validation tasks.\n"
                + json.dumps(payload, ensure_ascii=False, allow_nan=False)
            )
            result = self.launch(workspace, prompt, self.timeout)
            if result.returncode:
                raise RuntimeError("model role failed")
            value = json.loads((workspace / "response.json").read_text())
            if not isinstance(value, dict):
                raise TypeError("model role must return a JSON object")
            return value


class JSONCommandRole:
    """Stateless JSON stdin/stdout adapter for a globally configured model role."""

    def __init__(self, argv, *, version, timeout=600):
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("model role needs a fixed argv")
        self.argv, self.version, self.timeout = list(argv), version, timeout

    def __call__(self, payload):
        with tempfile.TemporaryDirectory(prefix="cosens-model-role-") as directory:
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("COSENS_", "MEMORY_", "FRIDAY_MEMORY_"))
            }
            result = subprocess.run(
                self.argv,
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                cwd=directory,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("model role command failed")
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise TypeError("model role must return one JSON object")
            return value


class ModelSpecBuilder:
    """Global natural-language compiler with a separate evidence reviewer.

    Calibration identifies an operator-validated compiler/reviewer release.
    This is not a universal domain oracle; ambiguous requirements must abstain.
    """

    def __init__(self, compiler, reviewer, *, version, calibration_id):
        self.compiler, self.reviewer = compiler, reviewer
        self.version, self.calibration_id = version, calibration_id

    def __call__(self, package, refs):
        from .evaluation_specs import KINDS, EvaluationNotReady

        evidence = {
            k: deepcopy(package[k])
            for k in ("task", "inputs", "deliverable", "acceptance", "feedback")
        }
        evidence["memory"] = deepcopy(package.get("criterion_memory", []))
        draft = self.compiler(
            {
                "role": "criterion_compiler",
                "instruction": "Construct executable external acceptance criteria from explicit requirements, original inputs, "
                "source-backed facts and attributable corrections. Never use an agent's answer as truth. "
                "Cover all material requirements. If unsupported, contradictory, or not expressible using the check kinds, "
                "return {status:'abstain',reason:...}. Otherwise return {status:'proposed',checks:[{key,title,path,kind,"
                "value,field?,tolerance?,critical,weight,source_refs:[exact allowed source ref]}]}. "
                "Allowed check kinds: " + ",".join(sorted(KINDS)),
                "evidence": evidence,
                "allowed_source_refs": refs,
            }
        )
        if draft.get("status") != "proposed":
            raise EvaluationNotReady(
                "criteria need clarification or a calibrated domain check"
            )
        checks = draft.get("checks")
        review = self.reviewer(
            {
                "role": "independent_criterion_reviewer",
                "instruction": "Review the proposed checks against the original evidence. Verify expected values, source support, "
                "requirement coverage and absence of agent-answer leakage. Return {approved:true} only if supported "
                "and complete; otherwise {approved:false,reason:...}. You have no candidate Recipe or execution results.",
                "evidence": evidence,
                "allowed_source_refs": refs,
                "checks": checks,
            }
        )
        if review.get("approved") is not True:
            raise EvaluationNotReady(
                "independent criterion review requires clarification"
            )
        return {
            "checks": checks,
            "review_sha256": fingerprint(
                {"evidence": evidence, "checks": checks, "review": review}
            ),
            "compiler_version": self.version,
            "calibration_id": self.calibration_id,
        }


@dataclass
class RuntimeProfile:
    version: str
    driver_factory: object
    isolated: bool = False
    proposer: object = propose_repairs
    spec_builder: object = None
    # An optional context provider must enforce experiment source allowlists;
    # ordinary production Memory search is never called during an experiment.
    context_provider: object = None
    policy: LearningPolicy = field(default_factory=lambda: LearningPolicy(max_rounds=1))
    budget: ExecutionBudget = field(default_factory=ExecutionBudget)
    train_rollouts: int = 2

    def contract(self):
        from dataclasses import asdict

        return {
            "runtime": self.version,
            "isolated": self.isolated,
            "policy": asdict(self.policy),
            "budget": asdict(self.budget),
            "train_rollouts": self.train_rollouts,
            "proposer": getattr(self.proposer, "version", "trajectory-seed-v1"),
            "spec_builder": getattr(self.spec_builder, "version", None),
            "spec_calibration": getattr(self.spec_builder, "calibration_id", None),
            "context_provider": getattr(self.context_provider, "version", None),
        }


def load_runtime():
    factory = os.environ.get("COSENS_RSI_GENERIC_RUNTIME")
    if not factory:
        return None
    module, separator, name = factory.partition(":")
    if not separator:
        raise ValueError("COSENS_RSI_GENERIC_RUNTIME must be module:factory")
    profile = getattr(importlib.import_module(module), name)()
    if (
        not isinstance(profile, RuntimeProfile)
        or not profile.version
        or not callable(profile.driver_factory)
    ):
        raise ValueError("global factory must return a versioned RuntimeProfile")
    if type(profile.isolated) is not bool or not 1 <= profile.train_rollouts <= 8:
        raise ValueError("invalid runtime isolation or rollout configuration")
    # Validate the same policy bounds used by the existing learner before any
    # task is executed. Do not accept per-family policies from public requests.
    fingerprint(profile.contract())
    return profile
