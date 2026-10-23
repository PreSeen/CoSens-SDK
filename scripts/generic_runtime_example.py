"""One deployment profile; no task-family registry, cases or business grader.

COSENS_RSI_GENERIC_RUNTIME=scripts.generic_runtime_example:build_runtime
COSENS_RSI_GENERIC_CONFIG=/absolute/path/to/runtime-profile.json

The operator must actually isolate the worker filesystem/network/credentials.
Setting isolated=true is an attestation, not an implementation of a sandbox.
"""

import json
import os
from functools import partial
from pathlib import Path

from cosens_rsi.algorithms.hierarchical import ExecutionBudget, fingerprint
from cosens_rsi.algorithms.hierarchical_learning import LearningPolicy
from cosens_rsi.algorithms.trajectory import propose_repairs
from cosens_rsi.runtime import (
    CodexJSONRole,
    CodexStepDriver,
    GenericAgentStepDriver,
    JSONCommandRole,
    ModelSpecBuilder,
    RuntimeProfile,
    run_codex,
)


def build_runtime():
    config = json.loads(Path(os.environ["COSENS_RSI_GENERIC_CONFIG"]).read_text())
    version = config["version"] + ":" + fingerprint(config)
    kind = config.get("kind", "command")
    if kind == "codex":
        launch = partial(
            run_codex,
            model=config.get("model", "gpt-6-luna"),
            reasoning=config.get("reasoning", "medium"),
        )
        driver = partial(CodexStepDriver, launch=launch)
        proposer = CodexJSONRole(version + ":proposer", launch=launch)
        compiler = CodexJSONRole(version + ":compiler", launch=launch)
        reviewer = CodexJSONRole(version + ":reviewer", launch=launch)
    elif kind == "command":
        argv = config["agent_argv"]
        if not argv or any(not isinstance(a, str) or not a for a in argv):
            raise ValueError("global agent argv required")
        driver = lambda workspace: GenericAgentStepDriver(workspace, argv)
        proposer = (
            JSONCommandRole(config["proposer_argv"], version=version + ":proposer")
            if config.get("proposer_argv")
            else propose_repairs
        )
        compiler = (
            JSONCommandRole(config["compiler_argv"], version=version + ":compiler")
            if config.get("compiler_argv")
            else None
        )
        reviewer = (
            JSONCommandRole(config["reviewer_argv"], version=version + ":reviewer")
            if config.get("reviewer_argv")
            else None
        )
    else:
        raise ValueError("generic runtime kind must be command or codex")
    builder = None
    if config.get("calibration_id"):
        if compiler is None or reviewer is None:
            raise ValueError("calibrated compiler requires an independent reviewer")
        builder = ModelSpecBuilder(
            compiler,
            reviewer,
            version=version + ":criteria",
            calibration_id=config["calibration_id"],
        )
    return RuntimeProfile(
        version,
        driver,
        isolated=config.get("isolated") is True,
        proposer=proposer,
        spec_builder=builder,
        policy=LearningPolicy(**config.get("learning_policy", {"max_rounds": 1})),
        budget=ExecutionBudget(**config.get("execution_budget", {})),
        train_rollouts=config.get("train_rollouts", 2),
    )
