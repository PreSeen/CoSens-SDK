"""Source-bound, immutable checks for the generic improvement pipeline.

These are external acceptance criteria, not a Recipe's self-checks. Unknown
semantics wait for a calibrated provider instead of silently passing an agent.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path

from .algorithms.hierarchical import fingerprint
from .evaluation import _safe_path


class EvaluationNotReady(ValueError):
    pass


KINDS = {"exists", "contains", "json_equal", "json_field_equal", "numeric_equal"}
ENGINE_VERSION = "generic-checks-v1"


def normalize_checks(checks, allowed_sources):
    if not isinstance(checks, list) or not checks or len(checks) > 100:
        raise EvaluationNotReady("non-empty source-bound acceptance criteria required")
    result, ids, expectations = [], set(), {}
    for index, original in enumerate(checks):
        if not isinstance(original, dict):
            raise EvaluationNotReady("invalid acceptance criterion")
        c = deepcopy(original)
        kind, path = c.get("kind"), c.get("path")
        if kind not in KINDS or not isinstance(path, str):
            raise EvaluationNotReady("criterion needs a calibrated check executor")
        if Path(path).is_absolute():
            raise EvaluationNotReady("criterion artifact paths must be relative")
        _safe_path(Path("/rsi-validation-root"), path)
        c.setdefault("key", "criterion-" + str(index + 1))
        c.setdefault("title", c["key"])
        c.setdefault("critical", True)
        c.setdefault("weight", 1)
        if (
            not isinstance(c["key"], str)
            or not c["key"]
            or c["key"] in ids
            or type(c["critical"]) is not bool
            or type(c["weight"]) not in (int, float)
            or not math.isfinite(c["weight"])
            or c["weight"] <= 0
            or not isinstance(c.get("source_refs"), list)
            or not c["source_refs"]
            or any(ref not in allowed_sources for ref in c["source_refs"])
        ):
            raise EvaluationNotReady(
                "criterion identity, severity, weight and source required"
            )
        if kind != "exists" and "value" not in c:
            raise EvaluationNotReady("criterion needs an explicit expected value")
        if kind == "contains" and (not isinstance(c["value"], str) or not c["value"]):
            raise EvaluationNotReady("contains requires text")
        if kind == "json_field_equal" and (
            not isinstance(c.get("field"), list)
            or any(
                not isinstance(x, (str, int)) or isinstance(x, bool) for x in c["field"]
            )
        ):
            raise EvaluationNotReady("JSON field requires an explicit field path")
        if kind == "numeric_equal" and (
            type(c["value"]) not in (int, float)
            or not math.isfinite(c["value"])
            or type(c.get("tolerance", 0)) not in (int, float)
            or not math.isfinite(c.get("tolerance", 0))
            or c.get("tolerance", 0) < 0
        ):
            raise EvaluationNotReady(
                "numeric checks require finite value and tolerance"
            )
        if kind in {"json_equal", "json_field_equal", "numeric_equal"}:
            identity = fingerprint([kind, path, c.get("field", [])])
            value = fingerprint(c["value"])
            if identity in expectations and expectations[identity] != value:
                raise EvaluationNotReady(
                    "contradictory acceptance criteria need clarification"
                )
            expectations[identity] = value
        ids.add(c["key"])
        result.append(c)
    return result


def build_spec(package, *, builder=None):
    """Compile explicit task requirements and attributable corrections once.

    A global, trusted builder may interpret natural language. Its calibration
    is a provider contract, never a flag supplied by an MCP caller. Arbitrary
    model-generated Python or shell tests are deliberately not supported.
    """
    refs = ["task:" + package["task_hash"]]
    checks = []
    for c in package["acceptance"].get("checks", []):
        checks.append({**c, "source_refs": [refs[0]]})
    for f in package["feedback"]:
        ref = "feedback:" + f["id"] + ":" + f["sha256"]
        refs.append(ref)
        # Only explicit task-bound corrections become expected answers. Free
        # text remains a hypothesis for the trusted builder, not a reward.
        for c in f["body"].get("acceptance_checks", []):
            checks.append({**c, "source_refs": [ref]})
    calibration = "builtin-declarative-checks-v1"
    review = None
    # Memory facts are available to the evaluator/compiler through precise
    # live-validated source spans. They are not ordinary actor recall.
    criterion_memory = []

    def memory_refs(value):
        if isinstance(value, dict):
            local = value.get("refs", value.get("source_refs", []))
            if isinstance(local, list) and value.get("text"):
                for r in local:
                    if (
                        isinstance(r, dict)
                        and {"source_id", "revision", "content_hash", "start", "end"}
                        <= r.keys()
                    ):
                        ref = "memory:" + fingerprint(r)
                        refs.append(ref)
                        criterion_memory.append(
                            {"ref": ref, "text": value["text"], "source": r}
                        )
            for v in value.values():
                memory_refs(v)
        elif isinstance(value, list):
            for v in value:
                memory_refs(v)

    memory_refs(package.get("snapshot", {}))
    refs = list(dict.fromkeys(refs))
    if builder is not None:
        if not getattr(builder, "version", None) or not getattr(
            builder, "calibration_id", None
        ):
            raise EvaluationNotReady(
                "criterion builder requires version and calibration"
            )
        view = deepcopy(package)
        view["criterion_memory"] = criterion_memory
        built = builder(view, list(refs))
        if isinstance(built, dict):
            checks = built.get("checks")
            review = built.get("review_sha256")
        else:
            checks = built
        calibration = builder.version + ":" + builder.calibration_id
    normalized = normalize_checks(checks, refs)
    spec = {
        "schema": "generic-evaluation-spec-v1",
        "checks": normalized,
        "engine": ENGINE_VERSION,
        "calibration": calibration,
        "scope": "declared_source_bound_requirements",
        "source_refs": refs,
    }
    if review:
        spec["independent_criterion_review_sha256"] = review
    # Including the complete expected set makes omissions invalid, even when
    # both arms accidentally omit the same check.
    spec["sha256"] = fingerprint(spec)
    return spec


def evaluate_spec(workspace, spec):
    expected = spec.get("sha256")
    if fingerprint({k: v for k, v in spec.items() if k != "sha256"}) != expected:
        raise ValueError("frozen evaluation specification changed")
    checks = normalize_checks(spec["checks"], spec["source_refs"])
    outcomes = []
    for c in checks:
        path = _safe_path(workspace, c["path"])
        passed = path.is_file()
        if passed and c["kind"] != "exists":
            try:
                text = path.read_text()
                if c["kind"] == "contains":
                    passed = c["value"] in text
                elif c["kind"] in {"json_equal", "json_field_equal"}:
                    value = json.loads(text)
                    for key in (
                        c.get("field", []) if c["kind"] == "json_field_equal" else []
                    ):
                        value = value[key]
                    passed = fingerprint(value) == fingerprint(c["value"])
                else:
                    value = float(text.strip())
                    passed = math.isfinite(value) and abs(value - c["value"]) <= c.get(
                        "tolerance", 0
                    )
            except (ValueError, UnicodeError, KeyError, IndexError, TypeError):
                passed = False
        outcomes.append(
            {
                "key": c["key"],
                "title": c["title"],
                "critical": c["critical"],
                "passed": bool(passed),
                "weight": c["weight"],
            }
        )
    failed = [c["title"] for c in outcomes if not c["passed"]]
    return {
        "judge_valid": True,
        "score": sum(c["weight"] for c in outcomes if c["passed"])
        / sum(c["weight"] for c in outcomes),
        "task_success": not failed,
        "quality_checks": outcomes,
        "criterion_manifest": [c["key"] for c in checks],
        "critical_failure": any(c["critical"] and not c["passed"] for c in outcomes),
        "feedback": "Source-bound checks: "
        + ("passed" if not failed else "; ".join(failed)),
    }
