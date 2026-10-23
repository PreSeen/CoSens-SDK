import json

from cosens_rsi.experience_policy import digest
from cosens_rsi.performance_observability import verified_supplier_measurement


def material():
    m = {'schema': 'supplier-reconciliation-performance-v1', 'kpis': {'exception_count': 12},
         'median_seconds': 1.0, 'sla_seconds': 2}
    content = json.dumps(m)
    ref = {'artifact_id': 'measurement', 'content_hash': digest(content)}
    evidence = {'schema': 'supplier-operator-evaluation-v1', 'run_id': 'r', 'decision_id': 'd',
                'measurement_artifact': ref, 'exact_rows': 12, 'exact_kpis': m['kpis'],
                'median_seconds': 1.0, 'sla_seconds': 2, 'includes_cold_index_build': True,
                'checks': dict.fromkeys(['input_lock_unchanged', 'committed_measurement_matches_disk',
                                        'all_three_csv_rows_exact', 'all_kpis_exact'], True)}
    text = json.dumps(evidence)
    run = {'id': 'r', 'artifacts': {'measurement': {'content': content, 'content_hash': ref['content_hash']},
                                   'evidence': {'content': text, 'content_hash': digest(text)}}}
    decision = {'id': 'd', 'evaluations': [{'status': 'verified', 'value': .5,
                'evidence_artifact_id': 'evidence', 'evidence_content_hash': digest(text)}]}
    return run, decision, m


def test_performance_card_requires_the_exact_evaluated_measurement():
    run, decision, m = material()
    assert verified_supplier_measurement(run, decision, m)
    assert not verified_supplier_measurement(run, decision, {**m, 'median_seconds': .001})
    decision['evaluations'][0]['value'] = 1.0
    assert not verified_supplier_measurement(run, decision, m)


def test_different_run_or_uncommitted_artifact_cannot_supply_the_gain():
    run, decision, m = material()
    run['id'] = 'another-run'
    assert not verified_supplier_measurement(run, decision, m)
    run['id'] = 'r'
    run['artifacts']['measurement']['content_hash'] = 'changed'
    assert not verified_supplier_measurement(run, decision, m)
