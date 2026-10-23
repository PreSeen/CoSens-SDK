import pytest

from memory import AuthContext, ExperienceService, MemoryService, Store
from cosens_rsi.service import MemoryDependencyError, RSIService


ACTIONS = [{"id": "retry", "description": "Retry the request", "confidence": 0.8},
           {"id": "inspect", "description": "Inspect the response schema", "confidence": 0.6}]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("COSENS_RSI_EXPERIENCE_MODE", "advisory")
    store = Store.sqlite(str(tmp_path / "memory.sqlite"))
    store.migrate()
    memory = MemoryService(store)
    actor = AuthContext("local", "rsi", True)
    memory.create_project(actor, "default", "Tests")
    experiences = ExperienceService(memory)
    rsi = RSIService(db_path=str(tmp_path / "rsi.sqlite"), memory_token="internal")
    rsi.runtime_mode = "manual"

    def bridge(method, path, body=None):
        if path == "/internal/v1/experiences/watermark":
            return {"watermark": experiences.watermark(actor, "default")}
        if path == "/internal/v1/wiki/search":
            return {"items": [], "coverage": {}}
        if path == "/internal/v1/recall":
            return {"items": []}
        if path == "/internal/v1/experiences/search":
            return experiences.search(actor, "default", **body)
        if path == "/internal/v1/experiences/upsert":
            return experiences.upsert(actor, "default", **body)
        if path.endswith("/outcomes"):
            return experiences.append_outcome(actor, "default", path.split("/")[-2], **body)
        raise AssertionError(path)

    rsi._memory = bridge
    yield rsi, experiences, actor, bridge
    store.close()


def prepare(rsi, objective="Diagnose response schema validation failure", **changes):
    return rsi.prepare_task({"objective": objective, "task_family": "api", **changes}, "owner")


def decide(rsi, run, **changes):
    return rsi.guidance(run["run_id"], "owner", "response schema validation failure", candidates=ACTIONS, **changes)


def act(rsi, run, decision, action="inspect"):
    return rsi.checkpoint(run["run_id"], "owner", {"decision_id": decision["decision_id"],
        "actual_action_id": action, "next_state": {"observed": "schema inspected"}})


def evaluate(rsi, run, decision, value=1, evaluation_id="eval-1", **changes):
    artifact = rsi.store_artifact("owner", "evaluation.json", '{"schema_check":"passed"}')
    body = {"evaluation_id": evaluation_id, "reward_spec": "verified-decision-return-v1",
            "status": "verified", "value": value, "evidence_artifact_id": artifact["artifact_id"],
            "evidence_content_hash": artifact["content_hash"], **changes}
    return rsi.evaluate_decision(run["run_id"], decision["decision_id"], body, admin_token=rsi.admin_token)


def test_actual_action_is_stored_in_memory_and_feedback_cannot_verify(runtime):
    rsi, experiences, actor, _ = runtime
    run = prepare(rsi)
    decision = decide(rsi, run)
    assert decision["decision"]["cold_start"]
    assert decision["decision"]["recommended_action_id"] == "retry"
    assert decide(rsi, run) == decision
    act(rsi, run, decision, "inspect")
    rsi.record_feedback(run["run_id"], "owner", {"type": "accepted", "reward": 1, "verified": True})
    assert rsi.flush_memory_outbox()["delivered"] == 1
    state = rsi.get_decision(run["run_id"], "owner", decision["decision_id"])
    saved = experiences.get(actor, "default", state["experience"]["experience_id"])
    assert saved["record"]["actual_action_id"] == "inspect"
    assert saved["record"]["outcome"]["status"] == "pending"
    assert saved["record"]["state"]["observation"] == "response schema validation failure"
    assert state["experience"]["revision"] == 1
    with rsi._db() as db:
        assert db.execute("SELECT payload_json FROM memory_outbox").fetchone()[0] == "{}"


def test_verified_outcomes_influence_only_new_compatible_runs(runtime):
    rsi, _, _, _ = runtime
    old = prepare(rsi, "Task prepared before experience was verified")
    for i, (action, value) in enumerate((("retry", -1), ("inspect", 1))):
        run = prepare(rsi, f"Diagnose response schema validation failure case {i}")
        decision = decide(rsi, run)
        act(rsi, run, decision, action)
        evaluate(rsi, run, decision, value, f"eval-{i}")
    assert rsi.flush_memory_outbox()["delivered"] == 4
    assert decide(rsi, old)["decision"]["cold_start"]
    current = decide(rsi, prepare(rsi))
    assert not current["decision"]["cold_start"]
    assert current["decision"]["recommended_action_id"] == "inspect"
    different = prepare(rsi, environment={"python": "different"})
    assert decide(rsi, different)["decision"]["cold_start"]


def test_recipe_changes_isolate_experience_without_changing_old_runs(runtime):
    rsi, _, _, _ = runtime
    for i, (action, value) in enumerate((("retry", -1), ("inspect", 1))):
        run = prepare(rsi)
        decision = decide(rsi, run)
        act(rsi, run, decision, action)
        evaluate(rsi, run, decision, value, f"recipe-scope-{i}")
    rsi.flush_memory_outbox()
    frozen = prepare(rsi)
    before = decide(rsi, frozen)
    assert before["decision"]["recommended_action_id"] == "inspect"
    rsi.bootstrap_seed("api", "introduce a new method contract")
    after = decide(rsi, prepare(rsi))
    assert after["decision"]["cold_start"]
    assert after["contract_hash"] != before["contract_hash"]
    assert decide(rsi, frozen) == before


def test_outbox_replays_after_lost_response_without_duplicate_versions(runtime):
    rsi, experiences, actor, bridge = runtime
    run = prepare(rsi)
    decision = decide(rsi, run)
    act(rsi, run, decision)
    evaluate(rsi, run, decision)
    failed = set()

    def lose_response(method, path, body=None):
        result = bridge(method, path, body)
        if path not in failed:
            failed.add(path)
            raise MemoryDependencyError()
        return result

    rsi._memory = lose_response
    rsi.flush_memory_outbox()
    # A different process can recover the persistent outbox after restart.
    recovered = RSIService(db_path=rsi.db_path, memory_token="internal")
    recovered._memory = lose_response
    for _ in range(3):
        with recovered._db() as db:
            db.execute("UPDATE memory_outbox SET available_at=0 WHERE state='retry_wait'")
        recovered.flush_memory_outbox()
    state = recovered.get_decision(run["run_id"], "owner", decision["decision_id"])
    assert state["experience"]["revision"] == 2
    assert all(w["state"] == "done" for w in state["writes"])
    assert experiences.db.one("SELECT COUNT(*) n FROM experience_versions")["n"] == 2


def test_outcomes_and_cross_run_records_are_authorized_and_atomic(runtime):
    rsi, _, _, _ = runtime
    run = prepare(rsi)
    decision = decide(rsi, run)
    other = prepare(rsi, "Another task")
    with pytest.raises(ValueError):
        act(rsi, other, decision)
    assert rsi.get_run(other["run_id"], "owner")["checkpoint"] == {}
    with pytest.raises(LookupError):
        rsi.get_decision(run["run_id"], "intruder", decision["decision_id"])
    with pytest.raises(PermissionError):
        rsi.evaluate_decision(run["run_id"], decision["decision_id"], {}, admin_token=rsi.token)
    with pytest.raises(ValueError):
        evaluate(rsi, run, decision)
    act(rsi, run, decision)
    act(rsi, run, decision)
    with rsi._db() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_outbox").fetchone()[0] == 1
    with pytest.raises(ValueError):
        act(rsi, run, decision, "retry")
    with pytest.raises(ValueError):
        evaluate(rsi, run, decision, evidence_content_hash="changed")


def test_guided_decisions_cannot_change_contract_or_progress(runtime):
    rsi, experiences, actor, _ = runtime
    rsi.bootstrap_seed("api", "test")
    run = prepare(rsi, execution_mode="guided")
    step = rsi.next_step(run["run_id"], "owner")
    with pytest.raises(ValueError):
        decide(rsi, run, node_id="wrong")
    with pytest.raises(ValueError):
        rsi.checkpoint(run["run_id"], "owner", {"guided_index": 99})
    with pytest.raises(ValueError):
        rsi.submit_result(run["run_id"], "owner", {"summary": "bypass steps"})
    decision = decide(rsi, run, node_id=step["node"]["id"])
    rsi.submit_step(run["run_id"], "owner", step["attempt_id"], step["attempt_token"],
                    {"brief": "Verified task and requirements"}, decision["decision_id"], "inspect")
    rsi.flush_memory_outbox()
    state = rsi.get_decision(run["run_id"], "owner", decision["decision_id"])
    record = experiences.get(actor, "default", state["experience"]["experience_id"])["record"]
    assert record["phase"] == step["node"]["id"]
    assert record["next_state"]["contract_status"] == "passed"
    assert record["outcome"]["status"] == "pending"


def test_expired_outbox_writer_is_fenced_and_recovered(runtime):
    rsi, experiences, actor, bridge = runtime
    run = prepare(rsi)
    decision = decide(rsi, run)
    act(rsi, run, decision)

    def pause_past_lease(method, path, body):
        result = bridge(method, path, body)
        with rsi._db() as db:
            db.execute("UPDATE memory_outbox SET lease_until=0 WHERE state='running'")
        return result

    rsi._memory = pause_past_lease
    assert rsi.flush_memory_outbox(limit=1)["delivered"] == 0
    assert rsi.get_decision(run["run_id"], "owner", decision["decision_id"])["experience"]["revision"] == 0
    rsi._memory = bridge
    assert rsi.flush_memory_outbox(limit=1)["delivered"] == 1
    assert experiences.db.one("SELECT COUNT(*) n FROM experience_versions")["n"] == 1


def test_outbox_attempt_limit_and_explicit_recovery(runtime):
    rsi, _, _, bridge = runtime
    run = prepare(rsi)
    decision = decide(rsi, run)
    act(rsi, run, decision)
    def unavailable(*args, **kwargs):
        raise MemoryDependencyError()
    rsi._memory = unavailable
    for _ in range(6):
        with rsi._db() as db:
            db.execute("UPDATE memory_outbox SET available_at=0")
        rsi.flush_memory_outbox()
    state = rsi.get_decision(run["run_id"], "owner", decision["decision_id"])
    write = state["writes"][0]
    assert write["state"] == "failed" and write["attempts"] == 5
    with pytest.raises(PermissionError):
        rsi.retry_memory_write(write["id"], admin_token=rsi.token)
    rsi.retry_memory_write(write["id"], admin_token=rsi.admin_token)
    rsi._memory = bridge
    assert rsi.flush_memory_outbox()["delivered"] == 1


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_policy_modes_and_frozen_configuration(runtime, monkeypatch, mode):
    rsi, _, _, _ = runtime
    for i, (action, value) in enumerate((("retry", -1), ("inspect", 1))):
        run = prepare(rsi, f"Training case {i}")
        decision = decide(rsi, run)
        act(rsi, run, decision, action)
        evaluate(rsi, run, decision, value, f"eval-{i}")
    rsi.flush_memory_outbox()
    monkeypatch.setenv("COSENS_RSI_EXPERIENCE_MODE", mode)
    run = prepare(rsi, "New inference task")
    monkeypatch.setenv("COSENS_RSI_EXPERIENCE_MODE", "advisory")
    decision = decide(rsi, run)["decision"]
    assert decision["selection_mode"] == mode
    assert decision["recommended_action_id"] == "retry"
    assert decision["cold_start"] == (mode == "off")
    if mode == "shadow":
        assert decision["experience_recommended_action_id"] == "inspect"
    else:
        before = sum(rsi.outbox_status().values())
        guidance = decide(rsi, run)
        act(rsi, run, guidance)
        assert sum(rsi.outbox_status().values()) == before
        with pytest.raises(ValueError, match="disabled"):
            evaluate(rsi, run, guidance, evaluation_id="off-evaluation")


def test_repeated_task_lineage_does_not_inflate_policy_support(runtime):
    rsi, experiences, actor, _ = runtime
    for i in range(3):
        run = prepare(rsi)
        decision = decide(rsi, run)
        act(rsi, run, decision)
        evaluate(rsi, run, decision, evaluation_id=f"repeated-{i}")
    rsi.flush_memory_outbox(limit=20)
    current = decide(rsi, prepare(rsi))
    inspect = next(s for s in current["decision"]["scores"] if s["action_id"] == "inspect")
    assert inspect["independent_tasks"] == 1


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 1.1, -0.1])
def test_invalid_candidate_confidence_rejected(runtime, bad):
    rsi, _, _, _ = runtime
    with pytest.raises(ValueError):
        rsi.guidance(prepare(rsi)["run_id"], "owner", candidates=[
            {"id": "bad", "description": "Invalid score", "confidence": bad}])
