"""Rewrite stored Execution rows and keep compact analysis metadata."""

from .progress import stage_progress


def compact_analysis(analysis):
    return {
        key: value
        for key, value in analysis.items()
        if key not in {"questions", "rubrics"}
    }


def upgrade_execution(execution):
    stored_rubrics = (
        execution.get("rubrics_version_id")
        if "base_execution_id" in execution
        else None
    )
    parent = execution.pop("parent_execution_id", None)
    execution.setdefault("base_execution_id", parent)
    execution["policy"].pop("auto_activate", None)
    if "progress_percent" not in execution:
        stage = (execution.get("error") or {}).get("stage", execution["stage"])
        execution["progress_percent"] = (
            100 if execution["status"] == "completed" else stage_progress(stage, 1, 1)
        )
    execution.pop("fixed_rubrics_version_id", None)
    execution.pop("source_session_id", None)
    result = execution.get("result")
    execution["rubrics_version_id"] = (
        execution.pop("resolved_rubrics_version_id", None)
        or (result or {}).get("rubrics_version_id")
        or stored_rubrics
    )
    for field in ("recipe_id", "recipe_version_id"):
        execution[field] = execution.get(field) or (result or {}).get(field)
    if result:
        execution["result"] = {
            "test_status": result["test_status"],
            "provisional": result["provisional"],
            "published": result.get("published", result.get("activated", False)),
        }
    for key, child_id in execution.pop("answer_children", {}).items():
        if key in execution["answers"]:
            execution["answers"][key]["continuation_execution_id"] = child_id
    execution.pop("hypothesis", None)
    if execution.get("analysis") is not None:
        execution["analysis"] = compact_analysis(execution["analysis"])
    return execution
