"""Bind supplier performance cards to the exact administrator-evaluated result."""
import hashlib
import json
import math


def verified_supplier_measurement(run, decision, measurement):
    if measurement.get('schema') != 'supplier-reconciliation-performance-v1':
        return False
    for evaluation in decision['evaluations']:
        if evaluation.get('status') != 'verified':
            continue
        evidence = run['artifacts'].get(evaluation.get('evidence_artifact_id'))
        if not evidence or evidence['content_hash'] != evaluation.get('evidence_content_hash'):
            continue
        try:
            data = json.loads(evidence['content'])
            ref = data['measurement_artifact']
            saved = run['artifacts'].get(ref['artifact_id'])
            if not saved or saved['content_hash'] != ref['content_hash'] or json.loads(saved['content']) != measurement:
                continue
            # Artifact hashes are over canonical JSON strings, as in store_artifact.
            canonical = json.dumps(saved['content'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            if ref['content_hash'] != 'sha256:' + hashlib.sha256(canonical).hexdigest():
                continue
            if data['schema'] != 'supplier-operator-evaluation-v1' or data['run_id'] != run['id'] or data['decision_id'] != decision['id']:
                continue
            if data['exact_kpis'] != measurement['kpis'] or data['exact_rows'] != measurement['kpis']['exception_count']:
                continue
            if data['median_seconds'] != measurement['median_seconds'] or data['sla_seconds'] != 2 or measurement['sla_seconds'] != 2:
                continue
            required = ['input_lock_unchanged', 'committed_measurement_matches_disk', 'all_three_csv_rows_exact', 'all_kpis_exact']
            if any(data['checks'].get(k) is not True for k in required) or data.get('includes_cold_index_build') is not True:
                continue
            reward = max(-1, min(1, (2 - measurement['median_seconds']) / 2))
            if type(evaluation.get('value')) not in (int, float) or not math.isclose(evaluation['value'], reward, abs_tol=1e-12):
                continue
            return True
        except (ValueError, TypeError, KeyError):
            continue
    return False
