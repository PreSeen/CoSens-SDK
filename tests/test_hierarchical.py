from copy import deepcopy
from pathlib import Path

import pytest

from cosens_rsi.algorithms.hierarchical import (
    ExecutionBudget, HierarchicalExecutor, bundle_from_flat_recipe, fingerprint, freeze_bundle, mutate, seed_bundle, validate_bundle,
)
from cosens_rsi.algorithms.hierarchical_learning import (
    BundleRegistry, HierarchicalLearner, LearningPolicy, compare, confirm_decision,
)


def edit(label="Check derived values using the saved artifact, then verify the final deliverable."):
    return {"layer": "step", "target": "verify", "slot": "steps", "value": [label]}


def test_step_edit_preserves_flow_and_pins_old_bundle():
    base = seed_bundle()
    frozen = freeze_bundle(base)
    candidate = mutate(base, edit())
    assert candidate["flow"] == base["flow"]
    assert candidate["bindings"]["verify"] != base["bindings"]["verify"]
    assert candidate["bindings"]["produce"] == base["bindings"]["produce"]
    assert freeze_bundle(base) == frozen
    assert freeze_bundle(candidate)["sha256"] != frozen["sha256"]


def test_flow_rewire_detects_cycle_and_port_mismatch():
    base = seed_bundle()
    with pytest.raises(ValueError, match="cycle"):
        mutate(base, {"layer": "flow", "op": "rewire", "node": "prepare", "value": {
            "inputs": {"task": "$task"}, "after": ["verify"]}})
    with pytest.raises(ValueError, match="incompatible"):
        mutate(base, {"layer": "flow", "op": "rewire", "node": "produce", "value": {
            "inputs": {"task": "$task", "brief": "verify.files"}, "after": []}})
    with pytest.raises(ValueError, match="redirect"):
        mutate(base, {"layer": "flow", "op": "remove", "node": "prepare", "value": {"redirects": {}}})


def test_flow_splice_insertion_and_removal_are_executable():
    base = seed_bundle()
    node = {"id": "refine", "goal": "Refine the preparation brief", "skill": "prepare",
            "inputs": {"task": "prepare.brief"}, "after": [], "max_repairs": 1}
    inserted = mutate(base, {"layer": "flow", "op": "insert", "node": "refine", "value": {
        "node": node, "consumer": "produce", "input_port": "brief", "output_port": "brief"}})
    assert [n["id"] for n in inserted["flow"]["nodes"]] == ["prepare", "refine", "produce", "verify"]
    removed = mutate(inserted, {"layer": "flow", "op": "remove", "node": "refine",
                               "value": {"redirects": {"brief": "prepare.brief"}}})
    assert removed["flow"]["nodes"] == base["flow"]["nodes"]


def test_flat_migration_keeps_instructions_without_claiming_learned_hierarchy():
    recipe = {"id": "old", "body": {"steps": ["Reconcile calculation units before comparing."],
              "checks": ["Verify sources."], "execution": {"budget": "limited"}}}
    baseline = bundle_from_flat_recipe(recipe)
    assert len(baseline["flow"]["nodes"]) == 1
    text = str(baseline)
    assert "Reconcile calculation units" in text and "Verify sources." in text and "limited" in text


def test_valid_flow_edit_keeps_all_step_versions():
    base = seed_bundle()
    candidate = mutate(base, {"layer": "flow", "op": "rewire", "node": "verify", "value": {
        "inputs": {"task": "$task", "files": "produce.files"}, "after": ["prepare"]}})
    assert candidate["flow"]["version"] != base["flow"]["version"]
    assert candidate["steps"] == base["steps"]
    assert candidate["bindings"] == base["bindings"]


class Driver:
    def __init__(self, root: Path, fail_first=False, transport=False):
        self.root, self.prompts = root, []
        self.fail_first, self.transport = fail_first, transport

    def execute(self, instruction, *, timeout):
        self.prompts.append(instruction)
        if self.transport:
            return {"completed": False, "error": "timeout", "outputs": {}}
        if '"brief": "text"' in instruction:
            value = {"brief": "requirements from source inputs"}
        elif self.fail_first and len(self.prompts) == 2:
            value = {"files": ["missing.docx"]}
        else:
            (self.root / "deliverable.txt").write_text("saved output")
            value = {"files": ["deliverable.txt"]}
        return {"completed": True, "outputs": value, "elapsed_sec": .01, "tokens": 10}


def test_executor_routes_only_passed_outputs_and_repairs_once(tmp_path):
    driver = Driver(tmp_path, fail_first=True)
    result = HierarchicalExecutor().run(seed_bundle(), "task request", tmp_path, driver)
    assert result["status"] == "completed"
    assert result["calls"] == 4
    assert [t["node"] for t in result["trace"]] == ["prepare", "produce", "produce", "verify"]
    assert result["trace"][1]["passed"] is False
    assert result["trace"][2]["passed"] is True
    assert '"repair_errors": []' not in driver.prompts[2]
    assert "requirements from source inputs" in driver.prompts[1]
    assert result["tokens"] == 40


def test_executor_normalizes_files_artifacts_alias_for_artifact_port(tmp_path):
    class ArtifactAliasDriver(Driver):
        def execute(self, instruction, *, timeout):
            self.prompts.append(instruction)
            if '"brief": "text"' in instruction:
                return {"completed": True, "outputs": {"brief": "requirements"}, "elapsed_sec": .01}
            (self.root / "deliverable.txt").write_text("saved output")
            return {"completed": True, "outputs": {"artifacts": ["deliverable.txt"]}, "elapsed_sec": .01}

    result = HierarchicalExecutor().run(seed_bundle(), "task request", tmp_path, ArtifactAliasDriver(tmp_path))
    assert result["status"] == "completed"
    assert result["final"] == ["deliverable.txt"]
    assert result["trace"][1]["outputs"] == {"files": ["deliverable.txt"]}
    assert result["trace"][1]["raw_outputs"] == {"artifacts": ["deliverable.txt"]}


def test_infrastructure_failure_does_not_become_recipe_failure(tmp_path):
    driver = Driver(tmp_path, transport=True)
    result = HierarchicalExecutor().run(seed_bundle(), "task", tmp_path, driver)
    assert result["status"] == "infrastructure_error"
    assert result["calls"] == 1
    assert result["failure"]["layer"] == "infrastructure"
    assert result["final"] is None


def test_budget_stops_before_downstream_node(tmp_path):
    driver = Driver(tmp_path)
    result = HierarchicalExecutor().run(seed_bundle(), "task", tmp_path, driver,
                                         ExecutionBudget(max_calls=1))
    assert result["status"] == "over_budget"
    assert len(driver.prompts) == 1


def test_artifact_cannot_be_an_unchanged_reference_or_escape(tmp_path):
    (tmp_path / "source.txt").write_text("input")
    class BadDriver(Driver):
        def execute(self, instruction, *, timeout):
            if '"brief": "text"' in instruction:
                return {"completed": True, "outputs": {"brief": "ok"}}
            return {"completed": True, "outputs": {"files": ["source.txt"]}}
    result = HierarchicalExecutor().run(seed_bundle(), "task", tmp_path, BadDriver(tmp_path))
    assert result["status"] == "failed"
    assert result["calls"] == 3
    assert result["trace"][-1]["errors"]


def records(bundle, ids, score=.7, valid=True):
    sha = freeze_bundle(bundle)["sha256"]
    return [{"task_id": i, "task_sha256": fingerprint(i), "bundle_sha256": sha,
             "protocol_sha256": "fixed-runtime-and-judge", "score": score, "judge_valid": valid,
             "elapsed_sec": 10, "feedback": "source evidence was not checked", "task_prompt": "Task " + i}
            for i in ids]


def test_invalid_judge_is_missing_evidence_not_quality_zero():
    base, candidate = seed_bundle(), mutate(seed_bundle(), edit())
    comparison = compare(records(base, ["a", "b"]),
                         records(candidate, ["a"]) + records(candidate, ["b"], valid=False),
                         ["a", "b"], base_sha=freeze_bundle(base)["sha256"], candidate_sha=freeze_bundle(candidate)["sha256"])
    assert comparison["coverage"] == .5
    assert comparison["mean_delta"] == 0
    assert confirm_decision(comparison, LearningPolicy(), "candidate")["status"] == "deferred"


def test_registry_rejects_same_version_with_changed_content(tmp_path):
    registry = BundleRegistry(tmp_path)
    base = seed_bundle()
    sha = registry.put(base)
    forged = deepcopy(base)
    forged["steps"]["verify@seed"]["body"]["steps"] = ["silently edited"]
    with pytest.raises(ValueError, match="immutable"):
        registry.put(forged)
    assert registry.get(sha) == validate_bundle(base)


def test_learning_loop_promotes_confirmed_bundle_and_next_round_uses_new_base(tmp_path):
    initial = seed_bundle()
    registry = BundleRegistry(tmp_path)
    partitions = {"train": ["learn-a"], "screen": ["screen-a", "screen-b"],
                  "confirm": [f"confirm-{i}" for i in range(8)]}
    seen, confirm_seen = [], []
    def propose(payload):
        assert len(payload["train_evidence"]) == 1
        assert not any("screen-a" in str(r) or "confirm-" in str(r) for r in payload["train_evidence"])
        seen.append(payload)
        return {"diagnosis": {"layer": "step", "reason": "unverified source"},
                "changes": [edit("Check sources " + str(len(seen))), edit("Check persisted values " + str(len(seen)))]}
    def evaluate(bundle, partition, ids):
        if partition == "confirm":
            confirm_seen.append(ids)
        # Successively improved versions have a stable, positive paired effect.
        generation = len(bundle["steps"]) - len(initial["steps"])
        return records(bundle, ids, score=.65 + generation * .1)
    result = HierarchicalLearner(registry, LearningPolicy(max_rounds=2)).run(
        "family", initial, partitions, propose, evaluate)
    assert len(result["rounds"]) == 2
    assert all(r["decision"]["status"] == "accepted" for r in result["rounds"])
    assert seen[1]["bundle"] != initial
    assert seen[1]["archive"]
    assert registry.active("family") == result["active"]
    assert set(confirm_seen[0]).isdisjoint(confirm_seen[2])
    assert registry.archive()


def test_confirm_cannot_be_the_screen_set(tmp_path):
    learner = HierarchicalLearner(BundleRegistry(tmp_path))
    with pytest.raises(ValueError, match="overlap"):
        learner.run("family", seed_bundle(), {"train": ["a"], "screen": ["b"], "confirm": ["b"]},
                    lambda _: {}, lambda *_: [])


def test_small_positive_confirm_defers_and_keeps_active(tmp_path):
    initial = seed_bundle()
    registry = BundleRegistry(tmp_path)
    def evaluate(bundle, partition, ids):
        return records(bundle, ids, .7 if bundle == initial else .8)
    result = HierarchicalLearner(registry, LearningPolicy(max_rounds=2)).run(
        "family", initial, {"train": ["a"], "screen": ["b"], "confirm": ["c"]},
        lambda _: {"changes": [edit("Inspect saved values"), edit("Reconcile all derived values")]}, evaluate)
    assert result["rounds"][0]["decision"]["status"] == "deferred"
    assert result["active"] == initial


def test_flow_learning_uses_same_gate_and_preserves_step_methods(tmp_path):
    initial = seed_bundle()
    def propose(_):
        return {"diagnosis": {"layer": "flow", "reason": "dependency ordering hypothesis"}, "changes": [
            {"layer": "flow", "op": "rewire", "node": "verify", "value": {
                "inputs": {"task": "$task", "files": "produce.files"}, "after": deps}}
            for deps in (["prepare"], ["prepare", "produce"])]}
    def evaluate(bundle, partition, ids):
        return records(bundle, ids, .7 if bundle == initial else .8)
    result = HierarchicalLearner(BundleRegistry(tmp_path), LearningPolicy(max_rounds=1)).run(
        "flow-arm", initial, {"train": ["learn"], "screen": ["screen"],
                              "confirm": [f"confirm-{i}" for i in range(4)]},
        propose, evaluate, allowed_layer="flow")
    assert result["rounds"][0]["decision"]["status"] == "accepted"
    assert result["active"]["steps"] == initial["steps"]
    assert result["active"]["flow"] != initial["flow"]


def test_two_negative_rounds_stop_and_feed_method_archive_back(tmp_path):
    initial = seed_bundle()
    payloads = []
    def propose(payload):
        payloads.append(payload)
        n = len(payloads)
        return {"changes": [edit(f"Inspect all evidence variant {n}"), edit(f"Inspect persisted data variant {n}")]}
    def evaluate(bundle, partition, ids):
        assert partition != "confirm"
        return records(bundle, ids, .8 if bundle == initial else .7)
    result = HierarchicalLearner(BundleRegistry(tmp_path)).run(
        "family", initial, {"train": ["learn"], "screen": ["screen"], "confirm": ["confirm"]}, propose, evaluate)
    assert len(result["rounds"]) == 2
    assert len(payloads[1]["archive"]) >= 2
    assert result["active"] == initial


def test_execution_uses_frozen_snapshot_even_if_caller_edits_bundle(tmp_path):
    bundle = seed_bundle()
    class MutatingCaller(Driver):
        def execute(self, instruction, *, timeout):
            bundle["flow"]["nodes"].clear()
            return super().execute(instruction, timeout=timeout)
    result = HierarchicalExecutor().run(bundle, "task", tmp_path, MutatingCaller(tmp_path))
    assert result["status"] == "completed"
    assert len(result["trace"]) == 3


def test_task_input_overwrite_is_rejected(tmp_path):
    (tmp_path / "reference.txt").write_text("original input")
    class SourceOverwriter(Driver):
        def execute(self, instruction, *, timeout):
            (tmp_path / "reference.txt").write_text("modified source")
            return super().execute(instruction, timeout=timeout)
    result = HierarchicalExecutor().run(seed_bundle(), "task", tmp_path, SourceOverwriter(tmp_path))
    assert result["status"] == "failed"
    assert "protected task input" in str(result["trace"][-1]["errors"])


def test_confirm_exposure_is_reserved_across_process_restarts(tmp_path):
    registry = BundleRegistry(tmp_path)
    registry.reserve_confirm("family", "candidate", ["confirm-a", "confirm-b"])
    reopened = BundleRegistry(tmp_path)
    assert reopened.used_confirm("family") == {"confirm-a", "confirm-b"}
    with pytest.raises(ValueError, match="already been exposed"):
        reopened.reserve_confirm("family", "different-candidate", ["confirm-a"])
    assert reopened.used_confirm("other-family") == set()


def test_invalid_screen_defers_without_negative_method_memory(tmp_path):
    initial = seed_bundle()
    registry = BundleRegistry(tmp_path)
    def evaluate(bundle, partition, ids):
        assert partition != "confirm"
        return records(bundle, ids, valid=(partition == "train" or bundle == initial))
    result = HierarchicalLearner(registry).run(
        "family", initial, {"train": ["learn"], "screen": ["screen"], "confirm": ["confirm"]},
        lambda _: {"changes": [edit("Inspect evidence mapping"), edit("Inspect persisted calculations")]}, evaluate)
    assert result["rounds"][0]["decision"]["status"] == "deferred"
    assert registry.archive("family") == []


def test_learning_can_switch_from_step_to_flow_after_promotion(tmp_path):
    initial = seed_bundle()
    layers = []
    def propose(payload):
        if payload["bundle"]["bindings"]["verify"] == "verify@seed":
            layers.append("step")
            return {"changes": [edit("Verify all persisted fields"), edit("Verify all evidence links")]}
        layers.append("flow")
        return {"changes": [{"layer": "flow", "op": "rewire", "node": "verify", "value": {
            "inputs": {"task": "$task", "files": "produce.files"}, "after": deps}}
            for deps in (["prepare"], ["prepare", "produce"])]}
    def evaluate(bundle, partition, ids):
        step_change = bundle["bindings"]["verify"] != "verify@seed"
        flow_change = bundle["flow"]["version"] != "seed"
        return records(bundle, ids, .65 + .1 * step_change + .1 * flow_change)
    result = HierarchicalLearner(BundleRegistry(tmp_path), LearningPolicy(max_rounds=2)).run(
        "adaptive", initial, {"train": ["learn"], "screen": ["screen"],
                              "confirm": [f"confirm-{i}" for i in range(8)]}, propose, evaluate)
    assert layers == ["step", "flow"]
    assert all(r["decision"]["status"] == "accepted" for r in result["rounds"])
    assert result["active"]["bindings"]["verify"] != "verify@seed"
    assert result["active"]["flow"]["version"] != "seed"


def test_no_evidenced_failure_stops_without_forcing_mutations(tmp_path):
    initial = seed_bundle()
    def evaluate(bundle, partition, ids):
        assert partition == "train"
        return records(bundle, ids, 1.0)
    result = HierarchicalLearner(BundleRegistry(tmp_path)).run(
        "family", initial, {"train": ["learn"], "screen": ["screen"], "confirm": ["confirm"]},
        lambda _: {"diagnosis": {"layer": "none", "reason": "no supported method defect"}, "changes": []}, evaluate)
    assert result["termination_reason"] == "no_learnable_failure"
    assert result["active"] == initial


def test_rollback_restores_whole_bundle_without_deleting_history(tmp_path):
    registry = BundleRegistry(tmp_path)
    initial = seed_bundle()
    registry.bootstrap("family", initial)
    candidate = mutate(initial, edit())
    sha = registry.put(candidate)
    registry.promote("family", freeze_bundle(initial)["sha256"], sha,
                     {"status": "accepted", "candidate_sha256": sha})
    assert registry.active("family") == candidate
    assert registry.rollback("family", "confirmed operational regression") == initial
    assert registry.active("family") == initial
    assert registry.get(sha) == candidate
    with pytest.raises(ValueError, match="reason"):
        registry.rollback("family", "")
    with pytest.raises(ValueError, match="prior promotion"):
        registry.rollback("family", "no further history")
