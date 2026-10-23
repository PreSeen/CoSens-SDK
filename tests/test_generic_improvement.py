"""Generic pipeline contracts using a real, deterministic file-operation runner.

This runner injects a known procedural defect; it tests orchestration/isolation
and publication, not language-model learning quality or generalization.
"""

import json
from copy import deepcopy
from typing import ClassVar

import pytest

from cosens_rsi.algorithms.hierarchical_learning import LearningPolicy, compare
from cosens_rsi.evaluation_specs import EvaluationNotReady, build_spec, evaluate_spec
from cosens_rsi.runtime import RuntimeProfile
from cosens_rsi.service import RSIService


class FileAgent:
    calls: ClassVar[list] = []

    def __init__(self, workspace):
        self.workspace = workspace

    def execute(self, instruction, *, timeout):
        invocation = json.loads(instruction.split("\n", 1)[1])
        self.calls.append(deepcopy(invocation))
        assert "tests" not in invocation["evidence_context"]
        assert "acceptance_checks" not in json.dumps(invocation)
        if "brief" in invocation["output_contract"]:
            return {
                "completed": True,
                "outputs": {
                    "brief": "Read the initial material and compute the requested total."
                },
            }
        data = json.loads((self.workspace / "input.json").read_text())
        method = json.dumps(invocation["method"])
        fixed = "Sum every input once" in method or "Accumulate all rows" in method
        total = sum(data["values"]) if fixed else data["values"][0]
        output = invocation["evidence_context"]["snapshot"]["deliverable"]["path"]
        (self.workspace / output).write_text(json.dumps({"total": total}))
        # No fabricated action-trace; quality improvement must still work.
        return {"completed": True, "outputs": {"files": [output]}}


def proposer(payload):
    proposer.calls.append(deepcopy(payload))
    assert len({r["task_id"] for r in payload["train_evidence"]}) == 1
    assert all(
        r["task_prompt"].startswith("Case 0 ") for r in payload["train_evidence"]
    )
    assert payload["diagnostic_context"]["cards"]
    card = payload["diagnostic_context"]["cards"][0]
    assert card["evidence_kind"] == "artifact_feedback"
    binding = card["binding"]
    body = payload["bundle"]["steps"][payload["bundle"]["bindings"][binding]]["body"]
    return {
        "diagnosis": {
            "layer": "step",
            "mechanism_id": card["mechanism_id"],
            "reason": "A partial aggregation fails the independent artifact check.",
        },
        "changes": [
            {
                "layer": "step",
                "target": binding,
                "slot": "steps",
                "value": body["steps"] + [rule],
            }
            for rule in (
                "Sum every input once before producing the final artifact.",
                "Accumulate all rows with exact arithmetic and verify completeness.",
            )
        ],
    }


proposer.calls = []


def service(tmp_path, monkeypatch):
    monkeypatch.delenv("COSENS_RSI_GENERIC_RUNTIME", raising=False)
    monkeypatch.delenv("COSENS_RSI_HARNESS_BACKEND", raising=False)
    rsi = RSIService(str(tmp_path / "service.sqlite"))

    def memory(method, path, body=None):
        if path.endswith("watermark"):
            return {"watermark": 0}
        if path.endswith("expand"):
            return {"validated_live": True, "items": [{"ref": r} for r in body["refs"]]}
        return {"items": []}

    rsi._memory = memory
    rsi.generic_runtime = RuntimeProfile(
        "test-isolated-file-agent-v1",
        FileAgent,
        isolated=True,
        proposer=proposer,
        policy=LearningPolicy(max_rounds=1),
        train_rollouts=1,
    )
    FileAgent.calls, proposer.calls = [], []
    return rsi


def task(rsi, index, *, family="unknown-family", owner="owner", acceptance=True):
    artifact = rsi.store_artifact(
        owner, "input.json", json.dumps({"values": [index + 1, index + 2]})
    )
    spec = {
        "objective": f"Case {index} sum every value from the attached original input.",
        "task_family": family,
        "input_artifact_ids": [artifact["artifact_id"]],
        "deliverable": {"path": "answer.json", "requires_files": True},
        "acceptance": {
            "checks": [
                {"path": "answer.json", "kind": "exists", "critical": False},
                {
                    "path": "answer.json",
                    "kind": "json_equal",
                    "value": {"total": 2 * index + 3},
                    "critical": False,
                },
            ]
        }
        if acceptance
        else {},
    }
    return rsi.prepare_task(spec, owner, explicit=True)


def process(rsi, job):
    assert rsi.process_one_improvement() == job["job_id"]
    return rsi.get_improvement(job["job_id"], "owner")


def test_unknown_family_prepares_validates_and_first_release_is_explicit(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    runs = [task(rsi, i) for i in range(6)]
    assert not rsi.improvement_backends
    assert rsi.get_active_recipe("unknown-family")["status"] == "not_found"
    rsi.record_feedback(
        runs[0]["run_id"],
        "owner",
        {"type": "correction", "message": "The aggregate omitted rows."},
    )
    rsi.capture_session_events(
        runs[0]["run_id"],
        "owner",
        [
            {
                "id": "tool-1",
                "kind": "tool_result",
                "payload": {"stdout": "partial aggregate"},
            }
        ],
    )
    job = rsi.start_improvement(
        runs[0]["run_id"], "owner", "Improve the aggregation procedure"
    )
    result = process(rsi, job)
    assert result["status"] == "validated", result
    assert result["report"]["release_decision"]["acceptance_path"] == "quality"
    assert result["report"]["evidence_level"] == "transfer_validated"
    assert len(result["report"]["manifest"]["partitions"]["confirm"]) == 4
    assert (
        proposer.calls[0]["train_evidence"][0]["expert_feedback"][0]["body"]["type"]
        == "correction"
    )
    assert any(
        e["id"] == "tool-1"
        for e in proposer.calls[0]["train_evidence"][0]["captured_events"]
    )
    assert rsi.get_active_recipe("unknown-family")["status"] == "not_found"
    with pytest.raises(PermissionError):
        rsi.publish_improvement(job["job_id"], None, "release", admin_token="wrong")
    with pytest.raises(ValueError, match="baseline"):
        rsi.publish_improvement(
            job["job_id"], 1, "release", admin_token=rsi.admin_token
        )
    published = rsi.publish_improvement(
        job["job_id"], None, "Independent checks passed", admin_token=rsi.admin_token
    )
    assert published["revision"] == 1
    assert (
        rsi.get_active_recipe("unknown-family")["recipe_id"] == published["recipe_id"]
    )
    assert task(rsi, 100)["recipe"]["recipe_id"] == published["recipe_id"]
    with pytest.raises(LookupError):
        rsi.get_improvement(job["job_id"], "other")


def test_waiting_resumes_same_candidates_after_restart_without_relearning(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    job = rsi.start_improvement(source["run_id"], "owner", "Improve")
    first = process(rsi, job)
    assert first["status"] == "awaiting_validation_tasks"
    assert first["report"]["preparation"]["candidate_count"] == 2
    assert len(proposer.calls) == 1
    old_hash = first["report"]["checkpoint_sha256"]
    reopened = RSIService(rsi.db_path)
    reopened._memory, reopened.generic_runtime = rsi._memory, rsi.generic_runtime
    for i in range(1, 6):
        task(reopened, i)
    final = process(reopened, job)
    assert final["status"] == "validated", final
    assert len(proposer.calls) == 1
    assert final["report"]["checkpoint_sha256"] != old_hash
    assert (
        len(
            list(
                (tmp_path / "harness-experiments" / job["job_id"] / "generations").glob(
                    "*/frozen-session.json"
                )
            )
        )
        == 3
    )


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("runner", "awaiting_runner"),
        ("isolation", "awaiting_runner"),
        ("judge", "awaiting_judge"),
        ("inputs", "awaiting_input"),
    ],
)
def test_missing_requirements_are_explicit_and_never_execute(
    tmp_path, monkeypatch, missing, expected
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0, acceptance=missing != "judge")
    if missing == "runner":
        rsi.generic_runtime = None
    elif missing == "isolation":
        rsi.generic_runtime.isolated = False
    elif missing == "inputs":
        run = rsi.prepare_task(
            {
                "objective": "Reconcile the missing original file",
                "task_family": "new",
                "deliverable": {"requires_files": True},
            },
            "owner",
            explicit=True,
        )
    result = process(rsi, rsi.start_improvement(run["run_id"], "owner", "Improve"))
    assert result["status"] == expected
    assert not FileAgent.calls


def test_explicit_correction_compiles_acceptance_without_cases_configuration(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0, acceptance=False)
    feedback = rsi.record_feedback(
        source["run_id"],
        "owner",
        {
            "message": "The total must include both rows",
            "acceptance_checks": [
                {
                    "path": "answer.json",
                    "kind": "json_equal",
                    "value": {"total": 3},
                    "critical": False,
                }
            ],
        },
    )
    package = rsi._compile_package(source["run_id"], "owner")
    spec = build_spec(package)
    assert spec["checks"][0]["source_refs"][0].startswith(
        "feedback:" + feedback["feedback_id"]
    )
    job = rsi.start_improvement(source["run_id"], "owner", "Improve")
    result = process(rsi, job)
    assert result["status"] == "awaiting_validation_tasks"
    assert result["report"]["preparation"]["candidate_count"] == 2


def test_capture_idempotency_conflicts_atomicity_and_authorization(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    e = {
        "id": "native-offset-17",
        "kind": "tool_result",
        "payload": {"text": "汉字\n" * 1000},
    }
    assert rsi.capture_session_events(run, "owner", [e]) == rsi.capture_session_events(
        run, "owner", [e]
    )
    with pytest.raises(ValueError, match="reused"):
        rsi.capture_session_events(
            run, "owner", [{**e, "id": "new-id"}, {**e, "payload": {"changed": True}}]
        )
    with rsi._db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM canonical_session_events WHERE event_id='new-id'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM canonical_session_events WHERE event_id=?",
                (e["id"],),
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(LookupError):
        rsi.capture_session_events(run, "other", [e])


def test_input_authorization_and_final_artifacts_do_not_replace_initial_inputs(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    private = rsi.store_artifact("other", "input.json", "private")
    with pytest.raises(LookupError):
        rsi.prepare_task(
            {"objective": "Read", "input_artifact_ids": [private["artifact_id"]]},
            "owner",
            explicit=True,
        )
    source = rsi.prepare_task(
        {"objective": "Read original file", "deliverable": {"requires_files": True}},
        "owner",
        explicit=True,
    )
    rsi.submit_result(source["run_id"], "owner", {"answer": "final answer only"})
    assert (
        rsi._compile_package(source["run_id"], "owner")["readiness"]
        == "hypothesis_only"
    )


def test_global_exposure_survives_job_family_and_run_renaming(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    original = task(rsi, 0)
    p = rsi._compile_package(original["run_id"], "owner")
    rsi._reserve_exposure([p], "old-job", "confirm", "old-candidate")
    renamed = task(rsi, 0, family="another-family")
    q = rsi._compile_package(renamed["run_id"], "owner")
    assert not rsi._package_unexposed(q, "new-job")
    with pytest.raises(ValueError, match="exposed"):
        rsi._reserve_exposure([q], "new-job", "train", "new-candidate")
    # The paired baseline/candidate are allowed under the exact locked receipt.
    rsi._reserve_exposure([p], "old-job", "confirm", "old-candidate")


def test_runtime_drift_and_running_jobs_cannot_be_silently_replayed(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    job = rsi.start_improvement(source["run_id"], "owner", "Improve")
    assert process(rsi, job)["status"] == "awaiting_validation_tasks"
    calls = len(FileAgent.calls)
    rsi.generic_runtime.version = "different-model-v2"
    rsi.resume_improvement(job["job_id"], "owner")
    assert process(rsi, job)["status"] == "awaiting_runner"
    assert len(FileAgent.calls) == calls
    with rsi._db() as db:
        db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["job_id"],))
    with pytest.raises(ValueError, match="waiting"):
        rsi.resume_improvement(job["job_id"], "owner")
    assert rsi.process_one_improvement() is None


def test_reused_confirm_cannot_validate_second_experiment(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    runs = [task(rsi, i) for i in range(6)]
    job = rsi.start_improvement(runs[0]["run_id"], "owner", "first")
    assert process(rsi, job)["status"] == "validated"
    second = rsi.start_improvement(runs[0]["run_id"], "owner", "second")
    assert process(rsi, second)["status"] == "awaiting_validation_tasks"


def test_release_rechecks_input_and_checkpoint_integrity(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    runs = [task(rsi, i) for i in range(6)]
    job = rsi.start_improvement(runs[0]["run_id"], "owner", "Improve")
    assert process(rsi, job)["status"] == "validated"
    with rsi._db() as db:
        db.execute(
            "UPDATE artifacts SET content='tampered' WHERE id=?",
            (runs[0]["task_package"]["input_artifact_ids"][0],),
        )
    with pytest.raises(LookupError, match="changed"):
        rsi.publish_improvement(
            job["job_id"], None, "release", admin_token=rsi.admin_token
        )


def test_builtin_checks_reject_unknown_semantics_and_tampering(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    package = rsi._compile_package(task(rsi, 0)["run_id"], "owner")
    spec = build_spec(package)
    (tmp_path / "answer.json").write_text('{"total": 3}')
    assert evaluate_spec(tmp_path, spec)["task_success"]
    altered = deepcopy(spec)
    altered["checks"].pop()
    with pytest.raises(ValueError, match="changed"):
        evaluate_spec(tmp_path, altered)
    package["acceptance"]["checks"] = [
        {"path": "answer.json", "kind": "execute_python", "value": "bad"}
    ]
    with pytest.raises(EvaluationNotReady):
        build_spec(package)


def test_complete_criterion_set_required_even_if_both_arms_omit_the_same_check():
    a = {
        "task_id": "x",
        "bundle_sha256": "a",
        "task_sha256": "fixed",
        "protocol_sha256": "fixed",
        "judge_valid": True,
        "score": 1,
        "task_success": True,
        "criterion_manifest": ["one", "two"],
        "quality_checks": [{"key": "one", "passed": True}],
    }
    result = compare(
        [a], [{**a, "bundle_sha256": "b"}], ["x"], base_sha="a", candidate_sha="b"
    )
    assert result["coverage"] == 0


def test_calibrated_global_builder_handles_free_text_without_family_code(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0, acceptance=False)
    package = rsi._compile_package(source["run_id"], "owner")

    def builder(p, refs):
        assert p["task"].startswith("Case 0")
        return [
            {
                "kind": "json_equal",
                "path": "answer.json",
                "value": {"total": 3},
                "source_refs": [refs[0]],
            }
        ]

    with pytest.raises(EvaluationNotReady, match="calibration"):
        build_spec(package, builder=builder)
    builder.version, builder.calibration_id = (
        "fixture-builder-v1",
        "checked-positive-negative-v1",
    )
    assert build_spec(package, builder=builder)["checks"][0]["value"] == {"total": 3}


def test_publication_requires_durable_confirmation_receipts(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    runs = [task(rsi, i) for i in range(6)]
    job = rsi.start_improvement(runs[0]["run_id"], "owner", "Improve")
    assert process(rsi, job)["status"] == "validated"
    with rsi._db() as db:
        db.execute("DELETE FROM experiment_exposures WHERE role='confirm'")
    with pytest.raises(ValueError, match="receipt"):
        rsi.publish_improvement(
            job["job_id"], None, "release", admin_token=rsi.admin_token
        )


def test_waiting_job_cannot_be_published(tmp_path, monkeypatch):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)
    job = rsi.start_improvement(run["run_id"], "owner", "Improve")
    assert process(rsi, job)["status"] == "awaiting_validation_tasks"
    with pytest.raises(ValueError, match="validated"):
        rsi.publish_improvement(
            job["job_id"], None, "release", admin_token=rsi.admin_token
        )
