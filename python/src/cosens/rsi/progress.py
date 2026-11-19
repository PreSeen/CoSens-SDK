"""Progress milestones for the execution lifecycle."""

_FIRST_ROUND = {"analyze": 10, "generate": 25, "test": 40, "evaluate": 55}
_RETRY_OFFSETS = {"generate": 10, "test": 20, "evaluate": 35}


def stage_progress(stage, round_number, max_rounds):
    offset = _RETRY_OFFSETS.get(stage)
    if round_number < 2 or max_rounds < 2 or offset is None:
        return _FIRST_ROUND.get(stage, 0)
    return 55 + int(((round_number - 2) * 35 + offset) / (max_rounds - 1) + 0.5)


def restart_execution(execution):
    execution["input_revision"] += 1
    execution["rubrics_version_id"] = None
    execution["stage"] = "analyze" if execution["status"] == "running" else "queued"
    execution["progress_percent"] = stage_progress(execution["stage"], 1, 1)
