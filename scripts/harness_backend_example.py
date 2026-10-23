"""Operator-owned factory; the JSON file describes tasks, never executable code.

Set COSENS_RSI_HARNESS_BACKEND=scripts.harness_backend_example:build_backends
and COSENS_RSI_HARNESS_CONFIG=/absolute/path/to/harness-config.json.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cosens_rsi.algorithms.hierarchical import ExecutionBudget
from cosens_rsi.algorithms.hierarchical_learning import LearningPolicy
from cosens_rsi.evaluation import CommandStepDriver, LocalHarnessBackend


def build_backends():
    config = json.loads(Path(os.environ["COSENS_RSI_HARNESS_CONFIG"]).read_text())
    result = {}
    for family, profile in config.items():
        argv = tuple(profile["agent_argv"])
        result[family] = LocalHarnessBackend(
            profile["cases"], lambda workspace, argv=argv: CommandStepDriver(workspace, list(argv)),
            driver_version=profile["driver_version"],
            policy=LearningPolicy(**profile.get("learning_policy", {})),
            budget=ExecutionBudget(**profile.get("execution_budget", {})),
            train_rollouts=profile.get("train_rollouts", 2))
    return result
