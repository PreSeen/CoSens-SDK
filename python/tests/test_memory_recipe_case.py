"""Protect the live case evaluator from plausible but incorrect reconciliations."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import importlib
import json

import pytest


PATH = Path(__file__).resolve().parents[1] / "examples/memory_recipe_case_fixture.py"
spec = importlib.util.spec_from_file_location("memory_recipe_case_fixture", PATH)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def correct_report(case, task):
    expected = case["expected"][task]
    return {
        "rows": [{"batch_id": bid, "expected_cents": v[0], "received_cents": v[1],
                  "due_date": v[2], "status": v[3], "evidence_ids": ["real-memory-id"]}
                 for bid,v in expected.items()],
        "duplicate_receipts": case["expected_duplicates"][task],
        "unmatched_receipts": case["expected_unmatched"][task],
        "summary_cents": {"expected_cents": sum(v[0] for v in expected.values()),
                          "received_cents": sum(v[1] for v in expected.values())},
    }


@pytest.mark.parametrize("task", ["task1", "task2"])
def test_reference_ledger_and_each_business_error_are_separately_detected(task):
    case = fixture.build_case("test-namespace")
    correct = correct_report(case, task)
    assert fixture.grade(case, task, correct, {"real-memory-id"})["strict_pass"]
    for row_index in range(len(correct["rows"])):
        for field, wrong_value in [("expected_cents", -1), ("received_cents", -1),
                                   ("due_date", "1900-01-01"), ("status", "unknown"),
                                   ("evidence_ids", ["invented-id"])]:
            broken = deepcopy(correct)
            broken["rows"][row_index][field] = wrong_value
            assert not fixture.grade(case, task, broken, {"real-memory-id"})["strict_pass"]


def test_same_amount_distinct_receipts_and_unknown_party_cannot_be_falsely_balanced():
    case = fixture.build_case("test-namespace")
    report = correct_report(case, "task2")
    report["rows"][0]["received_cents"] = 91000  # Wrongly deduplicate by amount.
    report["rows"][4].update(received_cents=39000, status="matched")
    failures = fixture.grade(case, "task2", report, {"real-memory-id"})["failed_keys"]
    assert "X-201:received_cents" in failures
    assert "X-205:received_cents" in failures
    assert "X-205:status" in failures


def test_complete_amounts_without_retrieved_evidence_are_not_a_memory_success():
    case = fixture.build_case("test-namespace")
    result = fixture.grade(case, "task2", correct_report(case, "task2"), set())
    assert not result["strict_pass"]
    assert len([k for k in result["failed_keys"] if k.endswith(":evidence")]) == 6


def test_feedback_does_not_invent_failure_when_first_attempt_passes():
    case = fixture.build_case("test-namespace")
    result = fixture.grade(case, "task1", correct_report(case, "task1"), {"real-memory-id"})
    assert "不虚构失败" in fixture.feedback_for(result)


def test_private_rules_and_reference_answers_are_not_inside_current_task_inputs():
    case = fixture.build_case("test-namespace")
    visible = fixture.dump(case["tasks"])
    for source, text in case["sources"]:
        assert text not in visible
    assert "第2个自然日" not in visible
    assert "R7表示已成功" not in visible
    assert "星渡渠道资金中心也对应青岚" not in visible


def test_report_extraction_preserves_json_and_records_format_failure(monkeypatch):
    monkeypatch.syspath_prepend(str(PATH.parent))
    runner = importlib.import_module("06_memory_recipe_transfer")
    value, strict = runner.parse_report('{"rows": []}')
    assert value == {"rows": []} and strict
    value, strict = runner.parse_report('Missing evidence.\n{"rows": []}')
    assert value == {"rows": []} and not strict
    with pytest.raises((ValueError, json.JSONDecodeError)):
        runner.parse_report('{"rows": [')
