import json

import pytest

from cosens_rsi.codex_trace import import_trace
from cosens_rsi.service import RSIService


ACTIONS = [
    {"id": "raw_xlsx", "version": "1", "description": "Read original records", "confidence": .5, "cost": 0.0},
    {"id": "indexed_sqlite", "version": "1", "description": "Read verified index", "confidence": .5, "cost": 0.0},
]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("COSENS_RSI_EXPERIENCE_MODE", "advisory")
    svc = RSIService(db_path=str(tmp_path / "rsi.sqlite"), memory_token="unused")

    def memory(method, path, body=None):
        if path.endswith("watermark"):
            return {"watermark": 1}
        if path.endswith("experiences/search"):
            return {"items": [], "watermark": 1}
        return {"items": [], "coverage": "test"}

    svc._memory = memory
    return svc


def task(service, objective, group="business-case", owner="owner", action="raw_xlsx", period="2011-11", seconds=10, learned=None):
    run = service.prepare_task({"objective": objective, "task_family": "business", "environment": {"case_id": group}}, owner)
    guidance = service.guidance(run["run_id"], owner, "same observation", candidates=ACTIONS)
    if learned:
        guidance["decision"]["cold_start"] = False
        guidance["decision"]["recommended_action_id"] = action
        guidance["decision"]["scores"][1]["experience_refs"] = [{"experience_id": learned, "revision": 2, "content_hash": "evidence"}]
        with service._db() as db:
            db.execute("UPDATE decisions SET response_json=? WHERE id=?", (json.dumps(guidance), guidance["decision_id"]))
    service.checkpoint(run["run_id"], owner, {"decision_id": guidance["decision_id"], "actual_action_id": action})
    measurement = {"period": period, "engine": action, "median_seconds": seconds,
        "trials": [{"exact_oracle_match": True}], "kpis": {"net_micros": 123000000},
        **{key: "fixed" for key in ["source_sha256", "cache_sha256", "engine_sha256", "measure_sha256", "oracle_sha256", "protocol_sha256"]}}
    artifact = service.store_artifact(owner, objective + ".json", json.dumps(measurement))
    service.submit_result(run["run_id"], owner, {"artifacts": [artifact]})
    return run, guidance, artifact


def comparison_case(service):
    before, _, _ = task(service, "基准")
    training, guide, _ = task(service, "校准", action="indexed_sqlite", period="2011-02", seconds=1)
    with service._db() as db:
        experience = db.execute("SELECT experience_id FROM decisions WHERE id=?", (guide["decision_id"],)).fetchone()[0]
        db.execute("UPDATE memory_outbox SET state='done',receipt_json=? WHERE decision_id=?",
                   (json.dumps({"reward_status": "verified", "revision": 2}), guide["decision_id"]))
    after, after_decision, artifact = task(service, "经验后", action="indexed_sqlite", seconds=1, learned=experience)
    case_id = service.list_cases("owner")[0]["case_id"]
    return case_id, before, after, after_decision, artifact


def test_case_grouping_is_explicit_and_principal_scoped(service):
    task(service, "甲", group="shared")
    task(service, "乙", group="shared")
    task(service, "别人的", group="shared", owner="other")
    first = service.prepare_task({"objective": "单独任务一"}, "owner")
    second = service.prepare_task({"objective": "单独任务二"}, "owner")
    cases = service.list_cases("owner")
    assert sorted(c["run_count"] for c in cases) == [1, 1, 2]
    grouped = next(c for c in cases if c["run_count"] == 2)
    assert len(service.get_case(grouped["case_id"], "owner")["runs"]) == 2
    with pytest.raises(LookupError):
        service.get_case(grouped["case_id"], "other")
    assert first["run_id"] != second["run_id"]


def test_comparison_requires_matching_contracts_inputs_and_verified_experience(service):
    cid, _, _, guidance, artifact = comparison_case(service)
    view = service.get_case(cid, "owner")
    assert view["comparison"]["available"]
    assert view["comparison"]["speedup"] == 10
    assert view["comparison"]["quality_equal"]
    assert [r["role"] for r in view["runs"]] == ["经验前基准", "经验校准", "使用经验后"]
    with service._db() as db:
        modified = json.loads(db.execute("SELECT content FROM artifacts WHERE id=?", (artifact["artifact_id"],)).fetchone()[0])
        modified["period"] = "different period"
        db.execute("UPDATE artifacts SET content=? WHERE id=?", (json.dumps(modified), artifact["artifact_id"]))
    assert not service.get_case(cid, "owner")["comparison"]["available"]


def test_comparison_does_not_treat_pending_experience_as_verified(service):
    cid, *_ = comparison_case(service)
    with service._db() as db:
        db.execute("UPDATE memory_outbox SET receipt_json=?", (json.dumps({"reward_status": "pending", "revision": 2}),))
    assert not service.get_case(cid, "owner")["comparison"]["available"]


def test_artifact_preview_is_case_scoped_and_json_is_projected(service):
    cid, _, _, _, artifact = comparison_case(service)
    preview = service.get_case_artifact(cid, artifact["artifact_id"], "owner")
    assert preview["kind"] == "record" and preview["highlights"]
    assert "content" not in preview
    stray = service.store_artifact("owner", "private.md", "not attached to this case")
    with pytest.raises(LookupError):
        service.get_case_artifact(cid, stray["artifact_id"], "owner")
    with pytest.raises(LookupError):
        service.get_case_artifact(cid, artifact["artifact_id"], "other")


def test_case_recipe_download_uses_run_frozen_revision_and_checks_case_scope(service):
    service.bootstrap_seed("business", "test seed")
    run = service.prepare_task({"objective": "Recipe trace", "task_family": "business",
                                "environment": {"case_id": "recipe-case"}}, "owner")
    case_id = service.list_cases("owner")[0]["case_id"]
    frozen = service.get_case_run_recipe(case_id, run["run_id"], "owner")
    service.bootstrap_seed("business", "later active seed")
    assert frozen["recipe_id"] == run["recipe"]["recipe_id"]
    assert frozen["revision"] == run["recipe"]["revision"]
    assert frozen["bundle"]["schema"] == "hierarchical-recipe-1"
    with pytest.raises(LookupError):
        service.get_case_run_recipe(case_id, run["run_id"], "other")
    service.prepare_task({"objective": "Different case", "environment": {"case_id": "other-case"}}, "owner")
    other_case = next(c["case_id"] for c in service.list_cases("owner") if c["case_id"] != case_id)
    with pytest.raises(LookupError):
        service.get_case_run_recipe(other_case, run["run_id"], "owner")


def test_trace_import_is_idempotent_and_suppresses_credentials(service, tmp_path):
    run, _, _ = task(service, "工具记录")
    path = tmp_path / "execution.jsonl"
    items = [
        {"type": "thread.started", "thread_id": "example-thread"},
        {"type": "item.completed", "item": {"id": "prepare", "type": "mcp_tool_call", "server": "cosens_rsi", "tool": "cosens_prepare_task", "arguments": {"objective": "任务"}, "result": {"content": [{"type": "text", "text": json.dumps({"run_id": run["run_id"], "status": "ready"})}]}}},
        {"type": "item.completed", "item": {"id": "command", "type": "command_execution", "command": "cat var/local.env", "aggregated_output": "COSENS_RSI_TOKEN=never-expose-this", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "next", "type": "mcp_tool_call", "server": "cosens_rsi", "tool": "cosens_next_step", "arguments": {}, "result": {"content": [{"type": "text", "text": json.dumps({"node": {"id": "produce", "goal": "创建文件"}, "attempt_token": "private-attempt"})}]}}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in items))
    import_trace(service, path, "owner")
    import_trace(service, path, "owner")
    with service._db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM execution_trace")]
    assert len(rows) == 3
    text = json.dumps(rows)
    assert "never-expose-this" not in text and "private-attempt" not in text
    assert "attempt_token" not in text


def attest_quality(service, run_id, passed, *, publish=True):
    source = service.store_artifact('owner', 'payment.py', 'def fixed_handler():\n    pass\n')
    with service._db() as db:
        row = db.execute('SELECT id,actual_action_id FROM decisions WHERE run_id=?', (run_id,)).fetchone()
        result = json.loads(db.execute('SELECT result_json FROM runs WHERE id=?', (run_id,)).fetchone()[0])
        result['source'] = source
        db.execute('UPDATE runs SET result_json=? WHERE id=?', (json.dumps(result), run_id))
    evidence = {'schema': 'code-repair-quality-v1', 'run_id': run_id, 'decision_id': row['id'],
        'action_id': row['actual_action_id'], 'source_artifact_id': source['artifact_id'],
        'source_content_hash': source['content_hash'], 'variant': 'holdout',
        **{key: 'fixed' for key in ['initial_source_sha256', 'requirements_sha256', 'suite_sha256', 'protocol_sha256', 'existing_tests_sha256']},
        'checks': [{'key': 'normal', 'title': '正常流程', 'passed': True}, {'key': 'replay', 'title': '乱序重放', 'passed': passed == 2}],
        'passed': passed, 'total': 2, 'failed': 2-passed}
    artifact = service.store_artifact('owner', 'quality.json', json.dumps(evidence))
    if publish:
        body = {'status': 'verified', 'value': 2*passed/2-1, 'evidence_artifact_id': artifact['artifact_id'], 'evidence_content_hash': artifact['content_hash']}
        with service._db() as db:
            db.execute('INSERT INTO decision_evaluations VALUES(?,?,?,?,?)', ('quality-'+run_id, row['id'], 'request-hash', json.dumps(body), 'test-outbox'))
    return artifact


@pytest.mark.parametrize('before_count,after_count', [(1, 2), (2, 2), (2, 1)])
def test_quality_comparison_uses_attested_source_and_keeps_failures_visible(service, before_count, after_count):
    cid, before, after, *_ = comparison_case(service)
    attest_quality(service, before['run_id'], before_count)
    artifact = attest_quality(service, after['run_id'], after_count)
    case = service.get_case(cid, 'owner')
    quality = case['comparison']
    assert quality['kind'] == 'code_quality'
    assert (quality['before_passed'], quality['after_passed']) == (before_count, after_count)
    assert quality['improved_checks'] == max(0, after_count-before_count)
    assert quality['regressed_checks'] == max(0, before_count-after_count)
    stages = case['runs'][0]['stages']
    assert next(s for s in stages if s['id'] == 'independent_acceptance')['status'] == ('failed' if before_count < 2 else 'passed')
    with service._db() as db:
        value = json.loads(db.execute('SELECT content FROM artifacts WHERE id=?', (artifact['artifact_id'],)).fetchone()[0])
        value['suite_sha256'] = 'different'
        updated = service.store_artifact('owner', 'changed-quality.json', json.dumps(value))
        body = {'status': 'verified', 'value': after_count-1, 'evidence_artifact_id': updated['artifact_id'], 'evidence_content_hash': updated['content_hash']}
        db.execute('UPDATE decision_evaluations SET body_json=? WHERE id=?', (json.dumps(body), 'quality-'+after['run_id']))
    # A different suite cannot establish quality improvement (the unrelated
    # latency fixture may still have its separate compatible comparison).
    assert service.get_case(cid, 'owner')['comparison'].get('kind') != 'code_quality'


def test_model_uploaded_quality_without_admin_attestation_is_not_trusted(service):
    cid, before, after, *_ = comparison_case(service)
    attest_quality(service, before['run_id'], 1, publish=False)
    attest_quality(service, after['run_id'], 2, publish=False)
    view = service.get_case(cid, 'owner')
    assert view['comparison'].get('kind') != 'code_quality'
    assert not any(s['id'] == 'independent_acceptance' for r in view['runs'] for s in r['stages'])


def test_managed_codex_trace_binding_requires_the_run_owner(service, tmp_path):
    run = service.prepare_task({'objective': 'Managed executor trace'}, 'owner')
    path = tmp_path/'managed.jsonl'
    path.write_text(json.dumps({'type':'item.completed','item':{'id':'tool','type':'command_execution',
        'command':'python3 reconcile.py','aggregated_output':'saved verified inputs','exit_code':0}})+'\n')
    imported = import_trace(service, path, 'owner', run_id=run['run_id'], node_id='review')
    assert imported['items_imported'] == 1
    with pytest.raises(LookupError):
        import_trace(service, path, 'other', run_id=run['run_id'], node_id='review')
