import json

import pytest

from cosens_rsi.algorithms.hierarchical import fingerprint, mutate, seed_bundle
from cosens_rsi.algorithms.hierarchical_learning import (
    BundleRegistry, HierarchicalLearner, LearningPolicy, compare, confirm_decision,
)
from cosens_rsi.algorithms.trajectory import diagnose, measure_trace, propose_repairs
from cosens_rsi.evaluation import CommandStepDriver, LocalHarnessBackend
from cosens_rsi.service import RSIService


def event(index, *, action="inspect", state="same", **changes):
    return {"id": f"e{index}", "sequence": index, "node": "produce", "kind": "action",
            "action": action, "input_hash": "input", "state_before": state, "state_after": state,
            "status": "passed", **changes}


def trace(count=4):
    return {"schema": "action-trace-v1", "complete": True,
            "events": [event(i) for i in range(1, count + 1)]}


def comparison(base_score=1, candidate_score=1, *, count=8, before_actions=8, after_actions=4,
               success=True, complete=True):
    base, candidate = [], []
    for i in range(count):
        common = {"task_id": f"t{i}", "task_sha256": fingerprint(i), "protocol_sha256": "fixed",
                  "judge_valid": True, "elapsed_sec": 10, "task_success": True}
        base.append({**common, "bundle_sha256": "base", "score": base_score, "action_trace": trace(before_actions)})
        candidate.append({**common, "bundle_sha256": "candidate", "score": candidate_score,
                          "task_success": success, "action_trace": {**trace(after_actions), "complete": complete}})
    return compare(base, candidate, [r["task_id"] for r in base], base_sha="base", candidate_sha="candidate")


def test_same_quality_shorter_successful_paths_are_accepted():
    result = confirm_decision(comparison(), LearningPolicy(), "candidate")
    assert result["status"] == "accepted"
    assert result["acceptance_path"] == "exploration"
    assert result["comparison"]["exploration"]["metrics"]["actions"]["mean_reduction"] == .5


@pytest.mark.parametrize("changes", [
    {"candidate_score": .98}, {"success": False}, {"complete": False},
    {"after_actions": 8}, {"count": 2},
])
def test_short_paths_do_not_bypass_quality_coverage_or_support(changes):
    assert confirm_decision(comparison(**changes), LearningPolicy(), "candidate")["status"] != "accepted"


def test_quality_path_does_not_require_action_telemetry_and_has_no_default_latency_gate():
    result = comparison(base_score=.7, candidate_score=.9, complete=False)
    result["max_latency_ratio"] = 100
    decision = confirm_decision(result, LearningPolicy(), "candidate")
    assert decision["acceptance_path"] == "quality"
    assert confirm_decision(result, LearningPolicy(max_latency_ratio=1.5), "candidate")["status"] == "rejected"


def test_noninferiority_requires_interval_not_only_small_average_loss():
    result = comparison()
    result["bootstrap_95_ci"] = [-.02, .02]
    assert confirm_decision(result, LearningPolicy(), "candidate")["status"] != "accepted"


@pytest.mark.parametrize("field", ["snapshot_sha256", "tests_sha256", "experience_policy_sha256", "session_sha256"])
def test_frozen_pair_drift_is_rejected(field):
    a = {"task_id": "a", "bundle_sha256": "base", "task_sha256": "same", "protocol_sha256": "same", field: "one"}
    b = {**a, "bundle_sha256": "candidate", field: "two"}
    with pytest.raises(ValueError, match="frozen"):
        compare([a], [b], ["a"], base_sha="base", candidate_sha="candidate")


def test_trace_checks_and_state_changes_are_not_redundant():
    value = trace()
    value["events"][1]["kind"] = "check"
    value["events"][2]["state_after"] = "new"
    assert measure_trace(value)["repeated_actions"] == 1
    for invalid in ({**value, "complete": False}, {**value, "events": [event(1), event(1)]}):
        with pytest.raises(ValueError):
            measure_trace(invalid)


def test_diagnosis_uses_multiple_train_rollouts_and_excludes_infrastructure():
    a = {"task_id": "learn", "action_trace": trace(3)}
    b = {"task_id": "learn", "action_trace": trace(1)}
    b["action_trace"]["events"][0].update(status="failed", error_kind="infrastructure")
    diagnosis = diagnose([{**a, "rollouts": [a, b]}], seed_bundle())
    assert len(diagnosis["cards"]) == 2
    assert all(c["kind"] == "repeated_no_progress" and c["rollout"] == 0 for c in diagnosis["cards"])
    assert not diagnose([{**a, "action_trace": {**trace(), "complete": False}}], seed_bundle())["cards"]


class FileDriver:
    """Actually performs and records repeated probes, then saves an artifact."""
    def __init__(self, root):
        self.root = root

    def execute(self, instruction, *, timeout):
        pack = json.loads(instruction.split("\n", 1)[1])
        method = json.dumps(pack["method"])
        events = []

        def observe(action, operation):
            def state():
                return fingerprint({p.name: p.read_text() for p in self.root.iterdir() if p.is_file()})
            before = state()
            result = operation()
            events.append(event(len(events) + 1, action=action, state=before, state_after=state()))
            return result

        if "brief" in pack["output_contract"]:
            value = observe("read-input", lambda: (self.root / "input.txt").read_text())
            outputs = {"brief": value}
        elif pack["goal"] == "Create requested deliverables":
            direct = any(word in method for word in ("Reuse an existing", "After an action", "Before exploration"))
            for _ in range(1 if direct else 4):
                observe("probe-format", lambda: (self.root / "input.txt").exists())
            observe("save-result", lambda: (self.root / "result.json").write_text('{"total": 6}'))
            outputs = {"files": ["result.json"]}
        else:
            observe("inspect-result", lambda: json.loads((self.root / "result.json").read_text()))
            outputs = {"files": ["result.json"]}
        return {"completed": True, "outputs": outputs, "tokens": 1,
                "action_trace": {"schema": "action-trace-v1", "complete": True, "events": events}}


def cases(confirm=4):
    return [{"id": f"{partition}-{i}", "lineage_id": f"lineage-{partition}-{i}", "partition": partition,
             "task": f"Calculate and save the total for dataset {partition} {i}.",
             "inputs": {"input.txt": "1,2,3"}, "snapshot": {"evidence": ["authorized reference"]},
             "tests": {"checks": [{"path": "result.json", "kind": "json_equal", "value": {"total": 6}}]}}
            for partition, size in (("train", 2), ("screen", 2), ("confirm", confirm)) for i in range(size)]


def backend(**kwargs):
    return LocalHarnessBackend(cases(), FileDriver, driver_version="file-driver-1",
                               policy=LearningPolicy(max_rounds=1), **kwargs)


def source_run():
    return {"snapshot": {"experience_context": {"watermark": 5, "policy": {"mode": "advisory"}}}}


def test_real_file_execution_diagnosis_generation_and_independent_confirmation(tmp_path):
    session = backend().open_session(source_run())
    original_hash = session.manifest["sha256"]
    seen = []

    def proposer(payload):
        seen.append(payload)
        assert "confirm-" not in str(payload)
        assert "screen-" not in str(payload)
        return propose_repairs(payload)

    result = HierarchicalLearner(BundleRegistry(tmp_path), session.policy).run(
        "files", seed_bundle(), session.partitions, proposer, session.evaluate)
    decision = result["rounds"][0]["decision"]
    assert decision["status"] == "accepted"
    assert decision["acceptance_path"] == "exploration"
    assert session.manifest["sha256"] == original_hash
    assert len(seen[0]["diagnostic_context"]["cards"]) == 12
    assert seed_bundle() != result["active"]
    # Methods and I/O evolve; the global acceptance contract stays fixed.
    assert result["active"]["flow"]["checks"] == seed_bundle()["flow"]["checks"]
    assert result["active"]["steps"]["produce@seed"] == seed_bundle()["steps"]["produce@seed"]


def test_tests_are_synthesized_once_and_hidden_from_actor_and_proposer():
    calls = []

    def synthesize(public):
        assert set(public) == {"task", "inputs"}
        calls.append(public)
        return {"checks": [{"path": "result.json", "kind": "contains", "value": "total"}]}

    configured = backend(test_synthesizer=synthesize)
    session = configured.open_session(source_run())
    assert len(calls) == len(cases())
    configured.cases[0]["snapshot"] = {"changed": True}
    session.evaluate(seed_bundle(), "screen", session.partitions["screen"])
    session.evaluate(seed_bundle(), "screen", session.partitions["screen"])
    assert len(calls) == len(cases())
    assert "total" not in str(session.manifest)
    with pytest.raises(ValueError, match="outside partition"):
        session.evaluate(seed_bundle(), "train", session.partitions["confirm"])


@pytest.mark.parametrize("field", ["lineage_id", "task"])
def test_suite_prevents_partition_leakage_via_duplicate_tasks(field):
    values = cases()
    values[-1][field] = values[0][field]
    with pytest.raises(ValueError, match="duplicate"):
        LocalHarnessBackend(values, FileDriver, driver_version="v1").open_session(source_run())


def service(tmp_path):
    rsi = RSIService(db_path=str(tmp_path / "rsi.sqlite"), memory_token="internal")
    rsi._memory = lambda method, path, body=None: {"watermark": 0, "items": []}
    rsi.bootstrap_seed("files", "explicit test seed")
    run = rsi.prepare_task({"objective": "Compute a file total", "task_family": "files"}, "owner")
    return rsi, run


def test_job_worker_keeps_production_isolated_and_publish_requires_revision_and_admin(tmp_path):
    rsi, run = service(tmp_path)
    baseline = rsi.get_active_recipe("files")
    rsi.improvement_backends["files"] = backend()
    job = rsi.start_improvement(run["run_id"], "owner", "reduce repeated format probing")
    assert job["status"] == "queued"
    with pytest.raises(LookupError):
        rsi.get_improvement(job["job_id"], "other")
    assert rsi.process_one_improvement() == job["job_id"]
    assert rsi.process_one_improvement() is None
    finished = rsi.get_improvement(job["job_id"], "owner")
    assert finished["status"] == "validated", finished
    audit = tmp_path / "harness-experiments" / job["job_id"]
    assert (audit / "frozen-session.json").is_file()
    pair = finished["report"]["release_decision"]["comparison"]["pairs"][0]
    saved = audit / "runs" / pair["candidate_evaluation_id"]
    assert json.loads((saved / "artifacts" / "result.json").read_text()) == {"total": 6}
    assert json.loads((saved / "record.json").read_text())["record"]["task_success"]
    assert rsi.get_active_recipe("files") == baseline
    with pytest.raises(PermissionError):
        rsi.publish_improvement(job["job_id"], 1, "release", admin_token=rsi.token)
    with pytest.raises(ValueError, match="changed"):
        rsi.publish_improvement(job["job_id"], 2, "release", admin_token=rsi.admin_token)
    published = rsi.publish_improvement(job["job_id"], 1, "confirmed shorter successful path", admin_token=rsi.admin_token)
    assert published["revision"] == 2
    assert rsi.get_active_recipe("files")["bundle_hash"] != baseline["bundle_hash"]
    assert rsi.get_run(run["run_id"], "owner")["recipe_id"] == baseline["recipe_id"]
    next_run = rsi.prepare_task({"objective": "Compute another file total", "task_family": "files"}, "owner")
    assert (rsi._learning_context(run["run_id"])["recipe_compatibility"] !=
            rsi._learning_context(next_run["run_id"])["recipe_compatibility"])
    with pytest.raises(ValueError, match="validated"):
        rsi.publish_improvement(job["job_id"], 2, "duplicate", admin_token=rsi.admin_token)


def test_unconfigured_and_approval_modes_do_not_execute(tmp_path):
    rsi, run = service(tmp_path)
    deferred = rsi.start_improvement(run["run_id"], "owner", "diagnose")
    assert deferred["status"] == "queued"
    assert rsi.process_one_improvement() == deferred["job_id"]
    assert rsi.get_improvement(deferred["job_id"], "owner")["status"] == "awaiting_runner"
    rsi.improvement_mode = "approval"
    assert rsi.start_improvement(run["run_id"], "owner", "diagnose")["status"] == "approval_required"
    assert rsi.process_one_improvement() is None


def test_failed_adapter_is_reported_and_never_published(tmp_path):
    rsi, run = service(tmp_path)

    class Broken:
        def open_session(self, run):
            raise RuntimeError("secret provider detail")

    rsi.improvement_backends["files"] = Broken()
    job = rsi.start_improvement(run["run_id"], "owner", "diagnose")
    rsi.process_one_improvement()
    result = rsi.get_improvement(job["job_id"], "owner")
    assert result["status"] == "failed" and result["error"] == "RuntimeError"
    assert "secret" not in json.dumps(result)
    with pytest.raises(ValueError, match="validated"):
        rsi.publish_improvement(job["job_id"], 1, "release", admin_token=rsi.admin_token)


def test_improvement_requests_are_atomic_idempotent_and_queued_jobs_survive_restart(tmp_path):
    rsi, run = service(tmp_path)
    rsi.improvement_backends["files"] = backend()
    first = rsi.start_improvement(run["run_id"], "owner", "diagnose", request_key="attempt-1")
    assert rsi.start_improvement(run["run_id"], "owner", "diagnose", request_key="attempt-1") == first
    with pytest.raises(ValueError, match="idempotency"):
        rsi.start_improvement(run["run_id"], "owner", "different request", request_key="attempt-1")
    reopened = RSIService(db_path=rsi.db_path, memory_token="unused")
    reopened.improvement_backends["files"] = backend()
    assert reopened.start_improvement(run["run_id"], "owner", "diagnose", request_key="attempt-1") == first
    assert reopened.process_one_improvement() == first["job_id"]
    assert reopened.get_improvement(first["job_id"], "owner")["status"] == "validated"
    with reopened._db() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE kind='learning'").fetchone()[0] == 1


def test_command_driver_executes_real_argv_and_json_protocol(tmp_path):
    import sys
    script = tmp_path / "actor.py"
    script.write_text('import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n'
                      'Path("saved.txt").write_text(p["instruction"])\n'
                      'print(json.dumps({"completed":True,"outputs":{"files":["saved.txt"]}}))\n')
    result = CommandStepDriver(tmp_path, [sys.executable, str(script)]).execute("save this", timeout=5)
    assert result["completed"] and (tmp_path / "saved.txt").read_text() == "save this"


def test_no_telemetry_does_not_generate_fictional_repairs():
    context = diagnose([{"task_id": "train", "trace": [{"tool": "short UI summary"}]}], seed_bundle())
    proposal = propose_repairs({"diagnostic_context": context, "allowed_layer": None})
    assert proposal["changes"] == []


def test_multiple_slots_require_a_supported_shared_mechanism(tmp_path):
    initial = seed_bundle()
    session = backend().open_session(source_run())

    def forged(payload):
        proposal = propose_repairs(payload)
        proposal["diagnosis"]["mechanism_id"] = "invented"
        return proposal

    with pytest.raises(ValueError, match="unsupported"):
        HierarchicalLearner(BundleRegistry(tmp_path), session.policy).run(
            "files", initial, session.partitions, forged, session.evaluate)


def test_candidate_removing_required_verification_cannot_pass_fixed_tests(tmp_path):
    # A candidate saving the wrong artifact must fail the same independent tests.
    class WrongDriver(FileDriver):
        def execute(self, instruction, *, timeout):
            result = super().execute(instruction, timeout=timeout)
            if "Inspect saved values" in instruction:
                (self.root / "result.json").write_text('{"total": 999}')
            return result

    session = LocalHarnessBackend(cases(), WrongDriver, driver_version="wrong-v1").open_session(source_run())
    candidate = mutate(seed_bundle(), {"layer": "step", "target": "produce", "slot": "steps",
                                       "value": ["Inspect saved values"]})
    records = session.evaluate(candidate, "screen", session.partitions["screen"])
    assert all(r["judge_valid"] and not r["task_success"] and r["score"] == 0 for r in records)


def test_successful_duplicate_node_is_pruned_only_after_independent_replay(tmp_path):
    initial = mutate(seed_bundle(), {"layer": "flow", "op": "insert", "node": "duplicate", "value": {
        "node": {"id": "duplicate", "goal": "Repeat input preparation", "skill": "prepare",
                 "inputs": {"task": "$task"}, "after": ["prepare"], "max_repairs": 1},
        "consumer": "produce", "input_port": "brief", "output_port": "brief"}})

    def propose_duplicate(payload):
        payload["diagnostic_context"]["cards"] = [c for c in payload["diagnostic_context"]["cards"]
                                                     if c["kind"] == "duplicate_node"]
        return propose_repairs(payload)

    session = backend(proposer=propose_duplicate).open_session(source_run())
    result = HierarchicalLearner(BundleRegistry(tmp_path), session.policy).run(
        "files", initial, session.partitions, session.propose, session.evaluate)
    assert result["rounds"][0]["decision"]["acceptance_path"] == "exploration"
    assert len(result["active"]["flow"]["nodes"]) == 3
    assert result["active"]["flow"]["checks"] == initial["flow"]["checks"]


@pytest.mark.parametrize("confirm_count,expected", [(8, "deferred"), (12, "validated")])
def test_chained_repairs_need_fresh_comparison_against_production(tmp_path, confirm_count, expected):
    class StageDriver(FileDriver):
        def execute(self, instruction, *, timeout):
            value = super().execute(instruction, timeout=timeout)
            pack = json.loads(instruction.split("\n", 1)[1])
            if pack["goal"] == "Create requested deliverables":
                output = self.root / "result.json"
                before = fingerprint(output.read_text())
                data = json.loads(output.read_text())
                for stage in ("one", "two"):
                    if "stage " + stage in json.dumps(pack["method"]):
                        data["stage_" + stage] = True
                output.write_text(json.dumps(data))
                events = value["action_trace"]["events"]
                events.append(event(len(events) + 1, action="save-stage-results", state=before,
                                    state_after=fingerprint(output.read_text())))
            return value

    def proposer(payload):
        body = payload["bundle"]["steps"][payload["bundle"]["bindings"]["produce"]]["body"]["steps"]
        stage = "two" if "stage one" in str(body) else "one"
        return {"changes": [{"layer": "step", "target": "produce", "slot": "steps",
                              "value": [*body, f"Save stage {stage} results using {method}."]}
                             for method in ("direct inspection", "explicit verification")]}

    values = cases(confirm=confirm_count)
    for case in values:
        case["tests"] = {"checks": [{"path": "result.json", "kind": "exists"},
                                     {"path": "result.json", "kind": "contains", "value": "stage_one"},
                                     {"path": "result.json", "kind": "contains", "value": "stage_two"}]}
    rsi, run = service(tmp_path)
    rsi.improvement_backends["files"] = LocalHarnessBackend(values, StageDriver, driver_version="stages-v1",
        policy=LearningPolicy(max_rounds=2), proposer=proposer)
    job = rsi.start_improvement(run["run_id"], "owner", "learn two complementary repairs")
    rsi.process_one_improvement()
    report = rsi.get_improvement(job["job_id"], "owner")
    assert report["status"] == expected, report
    assert len(report["report"]["result"]["rounds"]) == 2
    assert all(r["decision"]["status"] == "accepted" for r in report["report"]["result"]["rounds"])
    assert rsi.get_active_recipe("files")["revision"] == 1


def test_case_recipe_evolution_exposes_actual_pairs_and_guards_audit_artifacts(tmp_path):
    rsi, run = service(tmp_path)
    rsi.improvement_backends['files'] = backend()
    job = rsi.start_improvement(run['run_id'], 'owner', 'test actual evolution projection')
    rsi.process_one_improvement()
    case_id = rsi.list_cases('owner')[0]['case_id']
    view = rsi.get_case(case_id, 'owner')['recipe_evolution']
    assert view['available'] and len(view['pairs']) == 4
    assert view['before_recipe_id'] == run['recipe']['recipe_id']
    assert view['after_revision'] is None  # confirmation is not publication
    pair = view['pairs'][0]
    before, after = pair['before_trace'], pair['after_trace']
    assert before['source'] == after['source'] == 'harness_evaluation'
    assert before['trace_id'] != after['trace_id']
    assert before['bundle_hash'] == view['base_hash']
    assert after['bundle_hash'] == view['candidate_hash']
    assert len(rsi.get_case(case_id, 'owner')['runs']) == 1  # no invented after Run
    assert after['stages'][0]['inputs'][0]['text'] == pair['input_text']
    node = next(s for s in after['stages'] if s['id'].startswith('node:'))
    assert node['inputs'][0]['text'] != '未保存可读方法'
    assert node['outputs'][0]['text'] == '1,2,3'
    assert any(o.get('artifacts') for s in after['stages'] if s['id'].startswith('node:') for o in s['outputs'])
    assert after['stages'][-1]['title'] == '独立业务验收'
    assert after['quality']['passed'] == pair['after_passed']
    candidate = rsi.get_case_candidate_recipe(case_id, job['job_id'], 'owner')
    assert candidate['published'] is False
    assert candidate['bundle_hash'] != rsi.get_active_recipe('files')['bundle_hash']
    assert rsi.get_active_recipe('files')['recipe_id'] == run['recipe']['recipe_id']
    with pytest.raises(LookupError):
        rsi.get_case_candidate_recipe(case_id, job['job_id'], 'other')
    artifact = view['pairs'][0]['after_files'][0]['artifact_id']
    assert rsi.get_case_artifact(case_id, artifact, 'owner')['kind'] == 'record'
    with pytest.raises(LookupError):
        rsi.get_case_artifact(case_id, artifact, 'other')
    rsi.prepare_task({'objective': 'Unrelated audit case', 'task_family': 'files'}, 'owner')
    unrelated = next(x['case_id'] for x in rsi.list_cases('owner') if x['case_id'] != case_id)
    with pytest.raises(LookupError):
        rsi.get_case_artifact(unrelated, artifact, 'owner')
    parts = artifact.split(':')
    saved = tmp_path/'harness-experiments'/job['job_id']/'runs'/parts[2]/'artifacts/result.json'
    saved.write_text('tampered')
    with pytest.raises(LookupError, match='changed'):
        rsi.get_case_artifact(case_id, artifact, 'owner')
