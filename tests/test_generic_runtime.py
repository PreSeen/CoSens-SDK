"""Adapter, criterion provenance and live-read regression boundaries."""

import json
import re
from types import SimpleNamespace

import pytest
from test_generic_improvement import process, service, task

from cosens_rsi.algorithms.hierarchical import (
    HierarchicalExecutor,
    fingerprint,
    seed_bundle,
)
from cosens_rsi.evaluation_specs import EvaluationNotReady, build_spec, evaluate_spec
from cosens_rsi.runtime import (
    CodexStepDriver,
    GenericAgentStepDriver,
    JSONCommandRole,
    ModelSpecBuilder,
)


def test_codex_driver_follows_text_and_artifact_contracts_without_business_filenames(
    tmp_path,
):
    def launch(workspace, prompt, timeout):
        result_name = re.search(r"\.cosens-node-[0-9a-f]+\.json", prompt).group()
        packet = json.loads(prompt.split("\n", 2)[-1])
        outputs = {}
        for name, kind in packet["output_contract"].items():
            if kind == "text":
                outputs[name] = "A real preparation brief"
            else:
                target = "a-new-output.tsv"
                (workspace / target).write_text("name\tvalue\na\t1\n")
                outputs[name] = [target]
        (workspace / result_name).write_text(json.dumps({"outputs": outputs}))
        (workspace / "observable.jsonl").write_text(
            '{"type":"item.completed","item":{"type":"file_change"}}\n'
        )
        (workspace / "provider.stderr.log").write_text("")
        return SimpleNamespace(returncode=0)

    result = HierarchicalExecutor().run(
        seed_bundle(),
        "Produce an arbitrary TSV artifact",
        tmp_path,
        CodexStepDriver(tmp_path, launch=launch),
    )
    assert result["status"] == "completed", result
    assert result["final"] == ["a-new-output.tsv"]
    assert result["action_trace"]["complete"] is False
    assert all(step["audit_files"] for step in result["trace"])
    assert len(list(tmp_path.glob(".cosens-*-observable.jsonl"))) == 3


def test_shared_command_roles_strip_service_credentials(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("COSENS_RSI_ADMIN_TOKEN", "must-not-escape")
    monkeypatch.setenv("MEMORY_SECRET", "must-not-escape")
    monkeypatch.setenv("FRIDAY_MEMORY_SECRET", "legacy-must-not-escape")
    code = (
        "import json,os,sys; request=json.load(sys.stdin); "
        'assert not any(k.startswith(("COSENS_","MEMORY_","FRIDAY_MEMORY_")) for k in os.environ); '
        'print(json.dumps({"completed":True,"outputs":{"text":"ok"}}))'
    )
    argv = [sys.executable, "-c", code]
    assert GenericAgentStepDriver(tmp_path, argv).execute("node", timeout=5)[
        "completed"
    ]
    assert JSONCommandRole(argv, version="fixture-v1")({"role": "reviewer"})[
        "completed"
    ]


def test_independent_criterion_review_has_no_recipe_or_execution_and_can_abstain(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    package = rsi._compile_package(task(rsi, 0, acceptance=False)["run_id"], "owner")
    calls = []

    def compiler(payload):
        calls.append(payload)
        return {
            "status": "proposed",
            "checks": [
                {
                    "key": "sum",
                    "kind": "json_equal",
                    "path": "answer.json",
                    "value": {"total": 3},
                    "source_refs": [payload["allowed_source_refs"][0]],
                }
            ],
        }

    def reviewer(payload):
        assert (
            "bundle" not in payload
            and "result" not in payload
            and "events" not in payload["evidence"]
        )
        assert payload["role"] == "independent_criterion_reviewer"
        calls.append(payload)
        return {"approved": True}

    builder = ModelSpecBuilder(
        compiler, reviewer, version="criteria-v1", calibration_id="control-suite-v1"
    )
    spec = build_spec(package, builder=builder)
    assert len(calls) == 2
    assert spec["independent_criterion_review_sha256"]
    builder.reviewer = lambda payload: {
        "approved": False,
        "reason": "unsupported requirement",
    }
    with pytest.raises(EvaluationNotReady, match="review"):
        build_spec(package, builder=builder)


@pytest.mark.parametrize(
    "check,content,passed",
    [
        ({"kind": "contains", "value": "证据"}, "缺少依据", False),
        ({"kind": "contains", "value": "证据"}, "已核对证据", True),
        (
            {"kind": "json_field_equal", "field": ["rows", 0, "v"], "value": 3},
            '{"rows":[{"v":3}]}',
            True,
        ),
        (
            {"kind": "json_field_equal", "field": ["rows", 0, "v"], "value": 3},
            '{"rows":[]}',
            False,
        ),
        ({"kind": "json_equal", "value": True}, "1", False),
        ({"kind": "numeric_equal", "value": 1.25, "tolerance": 0.001}, "1.2501", True),
        ({"kind": "numeric_equal", "value": 1.25, "tolerance": 0.001}, "1.26", False),
        ({"kind": "numeric_equal", "value": 1.25}, "NaN", False),
    ],
)
def test_declarative_checks_positive_and_negative_controls(
    tmp_path, monkeypatch, check, content, passed
):
    rsi = service(tmp_path, monkeypatch)
    package = rsi._compile_package(task(rsi, 0)["run_id"], "owner")
    package["acceptance"]["checks"] = [{**check, "path": "output.dat"}]
    spec = build_spec(package)
    (tmp_path / "output.dat").write_text(content)
    result = evaluate_spec(tmp_path, spec)
    assert result["task_success"] is passed
    assert result["critical_failure"] is not passed


def test_conflicting_task_and_correction_criteria_wait_for_clarification(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    rsi.record_feedback(
        source["run_id"],
        "owner",
        {
            "acceptance_checks": [
                {"path": "answer.json", "kind": "json_equal", "value": {"total": 999}}
            ]
        },
    )
    package = rsi._compile_package(source["run_id"], "owner")
    with pytest.raises(EvaluationNotReady, match="contradictory"):
        build_spec(package)


def test_live_validation_batches_all_sources_and_revocation_blocks_run_reuse(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = task(rsi, 0)["run_id"]
    snapshot = rsi.get_run(run, "owner")["snapshot"]
    snapshot["evidence"] = [
        {
            "text": "source",
            "refs": [
                {
                    "source_id": "s" + str(i),
                    "revision": 1,
                    "content_hash": "hash",
                    "start": 0,
                    "end": 6,
                }
                for i in range(25)
            ],
        }
    ]
    with rsi._db() as db:
        db.execute(
            "UPDATE runs SET snapshot_json=?,snapshot_hash=? WHERE id=?",
            (json.dumps(snapshot), "sha256:" + fingerprint(snapshot), run),
        )
    sizes = []

    def memory(method, path, body):
        sizes.append(len(body["refs"]))
        return {"validated_live": True, "items": [{"ref": r} for r in body["refs"]]}

    rsi._memory = memory
    assert rsi.get_run(run, "owner")["snapshot"] == snapshot
    assert sizes == [12, 12, 1]

    def denied(*args):
        raise LookupError("source withdrawn")

    rsi._memory = denied
    with pytest.raises(LookupError, match="withdrawn"):
        rsi.get_run(run, "owner")
    with pytest.raises(LookupError):
        rsi.guidance(run, "owner")


def test_supplemented_inputs_wake_preparation_but_cannot_replace_frozen_inputs(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    run = rsi.prepare_task(
        {
            "objective": "Case 0 sum the original values",
            "task_family": "supplemented",
            "deliverable": {"requires_files": True, "path": "answer.json"},
            "acceptance": {
                "checks": [
                    {
                        "kind": "json_equal",
                        "path": "answer.json",
                        "value": {"total": 3},
                        "critical": False,
                    }
                ]
            },
        },
        "owner",
        explicit=True,
    )
    job = rsi.start_improvement(run["run_id"], "owner", "Improve")
    assert process(rsi, job)["status"] == "awaiting_input"
    a = rsi.store_artifact("owner", "input.json", '{"values":[1,2]}')
    result = rsi.supply_initial_inputs(run["run_id"], "owner", [a["artifact_id"]])
    assert result["basis"] == "supplemented_materials_not_historical_snapshot"
    assert process(rsi, job)["status"] == "awaiting_validation_tasks"
    with pytest.raises(ValueError, match="frozen"):
        rsi.supply_initial_inputs(run["run_id"], "owner", [a["artifact_id"]])


def test_existing_backend_global_confirm_reservations_survive_a_new_job(tmp_path):
    from test_harness import backend
    from test_harness import service as legacy_service

    rsi, run = legacy_service(tmp_path)
    rsi.improvement_backends["files"] = backend()
    job = rsi.start_improvement(run["run_id"], "owner", "first")
    assert process(rsi, job)["status"] == "validated"
    next_job = rsi.start_improvement(run["run_id"], "owner", "second")
    result = process(rsi, next_job)
    assert result["status"] == "deferred"
    assert (
        result["report"]["result"]["rounds"][0]["decision"]["reason"]
        == "too few independent confirm tasks"
    )


def test_correction_auto_policy_is_idempotent_and_does_not_change_rewards(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    rsi.improvement_mode = "auto"
    correction = {
        "type": "expert_correction",
        "message": "The aggregation omitted rows",
        "verified": True,
        "reward": 1,
    }
    first = rsi.record_feedback(source["run_id"], "owner", correction)
    second = rsi.record_feedback(source["run_id"], "owner", correction)
    assert first == second
    with rsi._db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM jobs WHERE kind='learning'").fetchone()[0]
            == 1
        )
        assert (
            db.execute("SELECT COUNT(*) FROM decision_evaluations").fetchone()[0] == 0
        )
    rsi.improvement_mode = "approval"
    response = rsi.record_feedback(
        source["run_id"], "owner", {**correction, "message": "Another correction"}
    )
    assert "improvement" not in response


def test_new_evidence_arriving_during_preparation_cannot_lose_its_wakeup(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    original = rsi.generic_runtime.proposer

    def arriving(payload):
        proposal = original(payload)
        # The worker's original catalog snapshot lacks this task. Its arrival
        # occurs while the job is running, before the worker waits for tasks.
        task(rsi, 1)
        return proposal

    rsi.generic_runtime.proposer = arriving
    job = rsi.start_improvement(source["run_id"], "owner", "Improve")
    first = process(rsi, job)
    assert first["status"] == "queued"
    second = process(rsi, job)
    assert second["status"] == "awaiting_validation_tasks"
    assert len(second["report"]["manifest"]["partitions"]["screen"]) == 1
    assert len(original.calls) == 1


def test_declared_cross_session_lineage_does_not_become_fresh_after_paraphrasing(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    runs = [
        rsi.prepare_task(
            {
                "objective": text,
                "task_family": "linked",
                "task_lineage_id": "one-real-task",
            },
            "owner",
            explicit=True,
        )
        for text in ("Original request", "Paraphrased continuation")
    ]
    a, b = [rsi._compile_package(r["run_id"], "owner") for r in runs]
    rsi._reserve_exposure([a], "experiment", "train", "base")
    assert not rsi._package_unexposed(b, "another-experiment")


def test_a_new_correction_after_freezing_requires_a_new_experiment(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    source = task(rsi, 0)
    package = rsi._compile_package(source["run_id"], "owner")
    rsi.record_feedback(
        source["run_id"], "owner", {"message": "A newly discovered requirement"}
    )
    with pytest.raises(LookupError, match="new frozen"):
        rsi._validate_package(package)


def test_editable_working_copy_does_not_mutate_the_immutable_input(
    tmp_path, monkeypatch
):
    rsi = service(tmp_path, monkeypatch)
    original = rsi.store_artifact("owner", "script.py", "print('before')\n")
    source = rsi.prepare_task(
        {
            "objective": "Change the saved program output",
            "task_family": "code-edit",
            "input_artifact_ids": [original["artifact_id"]],
            "deliverable": {"editable_inputs": ["script.py"]},
            "acceptance": {
                "checks": [{"kind": "contains", "path": "script.py", "value": "after"}]
            },
        },
        "owner",
        explicit=True,
    )
    p = rsi._compile_package(source["run_id"], "owner")
    case = rsi._generic_case(p, "train", rsi.generic_runtime)
    assert case["editable_inputs"] == ["script.py"]

    class EditDriver:
        def execute(self, instruction, *, timeout):
            pack = json.loads(instruction.split("\n", 1)[1])
            if "brief" in pack["output_contract"]:
                return {
                    "completed": True,
                    "outputs": {"brief": "Edit the saved source file"},
                }
            (tmp_path / "script.py").write_text("print('after')\n")
            return {"completed": True, "outputs": {"files": ["script.py"]}}

    (tmp_path / "script.py").write_text(p["inputs"]["script.py"])
    result = HierarchicalExecutor().run(
        seed_bundle(),
        p["task"],
        tmp_path,
        EditDriver(),
        editable_inputs=case["editable_inputs"],
    )
    assert result["status"] == "completed"
    with rsi._db() as db:
        assert (
            db.execute(
                "SELECT content FROM artifacts WHERE id=?", (original["artifact_id"],)
            ).fetchone()[0]
            == "print('before')\n"
        )
