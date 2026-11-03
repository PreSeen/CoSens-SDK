"""Supplemental paired Memory ablation on the same task2, retaining original failures.

Both conditions expose the exact same tools, model, disabled-thinking setting,
prompt, input, token cap and turn cap. The host withholds actual retrieved evidence
in one condition. This is a Memory dependency check, never Recipe migration.
"""
from pathlib import Path
import os
import time

from dotenv import load_dotenv

from importlib import import_module
runner = import_module("06_memory_recipe_transfer")


def main():
    load_dotenv(runner.ROOT / ".env", override=False)
    e = runner.Experiment(runner.ROOT / "python/docs/testing/latest-memory-recipe-20261102")
    try:
        if not e.state.get("supplemental_protocol"):
            e.state["supplemental_protocol"] = {
                "registered_before_supplemental_runs_at": time.time(),
                "reason": "Original no-Memory execution used its token budget in reasoning without delivering an answer; do not score as a business-content failure.",
                "same_business_task_id": e.state["calls"]["task2:task"]["response"]["task_id"],
                "arms": ["supplemental_memory_hidden", "supplemental_memory_visible"],
                "tools_identical": True, "model": os.environ["OPENAI_MODEL"], "thinking": "disabled",
                "max_tokens_per_call": 6500, "max_model_turns": 12,
                "only_arm_difference": "real SDK Memory evidence withheld or visible to actor",
                "recipe": None, "interpretation": "Memory dependency only; no Recipe improvement claim"}
            e.save()
        options = {"model_extra_body": {"thinking": {"type": "disabled"}},
                   "output_instruction": "若Memory未返回必要证据，立即把无法判断的字段置null、status用unknown；不要猜测。notes控制在300字内。"}
        task = e.state["calls"]["task2:task"]["response"]
        for masked, arm in [(True, "supplemental_memory_hidden"), (False, "supplemental_memory_visible")]:
            e.mask_memory_for_control = masked
            e.execute("task2", arm, task, memory=True, actor_options=options)
        e.state["supplemental_status"] = "completed"
        e.save()
    finally:
        e.sdk.close()


if __name__ == "__main__":
    main()
