"""Operator-only evaluator. Codex/MCP never receives the administrator credential."""
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]
BASE = 'http://127.0.0.1:18104'


def check_inputs():
    for name, digest in json.loads((ROOT / 'input-lock.json').read_text()).items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Frozen input changed: ' + name)


def main():
    check_inputs()
    protocol = json.loads((ROOT / 'protocol.json').read_text())
    oracle = json.loads((ROOT / 'data/oracle.json').read_text())
    runs = json.loads((ROOT / 'output/stage1_runs.json').read_text())
    execution_log = PROJECT / 'var/retail-stage1-events.jsonl'
    executed = []
    for line in execution_log.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get('item', {})
        if event.get('type') == 'item.completed' and item.get('type') == 'command_execution' and item.get('exit_code') == 0:
            executed.append(item.get('command', ''))
    config = dict(line.split('=', 1) for line in (PROJECT / 'var/local.env').read_text().splitlines() if '=' in line)
    ordinary = {'Authorization': 'Bearer ' + config['COSENS_RSI_TOKEN']}
    administrator = {'Authorization': 'Bearer ' + config['COSENS_RSI_ADMIN_TOKEN']}
    results = []
    with httpx.Client(base_url=BASE, timeout=20) as client:
        def api(method, path, payload=None, admin=False):
            response = client.request(method, path, json=payload, headers=administrator if admin else ordinary)
            response.raise_for_status()
            return response.json()

        for label, engine in [('training_raw', 'raw_xlsx'), ('training_indexed', 'indexed_sqlite')]:
            run = runs[label]
            measurement_path = ROOT / run['measurement']
            measurement = json.loads(measurement_path.read_text())
            if measurement['engine'] != engine or measurement['period'] != protocol['training'][engine]:
                raise RuntimeError('Wrong training engine/period')
            invocation = next((command for command in executed if all(part in command for part in
                ['measure.py --engine ' + engine, '--period ' + protocol['training'][engine], '--out ' + run['measurement']])), None)
            if invocation is None:
                raise RuntimeError('No successful tool execution in Codex event log for this measurement')
            for field, path in [('engine_sha256', 'engine.py'), ('measure_sha256', 'measure.py'),
                                ('oracle_sha256', 'data/oracle.json'), ('protocol_sha256', 'protocol.json')]:
                if measurement[field] != hashlib.sha256((ROOT / path).read_bytes()).hexdigest():
                    raise RuntimeError('Measurement code/input identity mismatch')
            if measurement['source_sha256'] != oracle['source_sha256']:
                raise RuntimeError('Dataset mismatch')
            if measurement['kpis'] != oracle['periods'][measurement['period']]:
                raise RuntimeError('Independent business ledger reconciliation failed')
            trials = measurement['trials']
            if len(trials) != protocol['repeats'] or any(
                    not t['exact_oracle_match'] or not isinstance(t['seconds'], (float, int))
                    or not math.isfinite(t['seconds']) or t['seconds'] <= 0 for t in trials):
                raise RuntimeError('Invalid execution measurements')
            median = statistics.median(t['seconds'] for t in trials)
            value = 1 - 2 * min(median / protocol['reward']['latency_budget_seconds'], 1)
            if not math.isclose(value, measurement['reward'], abs_tol=1e-12):
                raise RuntimeError('Reward does not match predeclared formula')
            state = api('GET', '/v1/runs/' + run['run_id'])
            decision_path = '/v1/runs/' + run['run_id'] + '/decisions/' + run['decision_id']
            decision = api('GET', decision_path)
            if state['status'] != 'completed' or decision['actual']['actual_action_id'] != engine:
                raise RuntimeError('Run is incomplete or actual action differs from measurement')
            evidence = {'schema': 'retail-independent-evaluation-v1', 'evaluated_at': datetime.now(timezone.utc).isoformat(),
                        'run_id': run['run_id'], 'decision_id': run['decision_id'],
                        'claim': 'Exact business ledger/count agreement with independently parsed OOXML oracle; measured process wall latency.',
                        'not_claimed': ['business advice quality', 'user acceptance', 'unrestricted Codex superiority', 'general algorithm gain'],
                        'protocol': protocol['reward'], 'measurement_file_sha256': hashlib.sha256(measurement_path.read_bytes()).hexdigest(),
                        'executed_command': invocation, 'codex_event_log_sha256': hashlib.sha256(execution_log.read_bytes()).hexdigest(),
                        'measurement': measurement, 'recomputed_value': value}
            artifact = api('POST', '/v1/artifacts', {'name': label + '-independent-evaluation.json',
                           'content': json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)})
            evaluation_id = 'retail-' + run['decision_id']
            receipt = api('POST', decision_path.replace('/v1/runs/', '/internal/admin/v1/runs/') + '/evaluations',
                {'evaluation_id': evaluation_id, 'reward_spec': protocol['reward']['spec'], 'status': 'verified',
                 'value': value, 'evidence_artifact_id': artifact['artifact_id'],
                 'evidence_content_hash': artifact['content_hash']}, admin=True)
            for _ in range(100):
                decision = api('GET', decision_path)
                if len(decision['writes']) >= 2 and all(w['state'] == 'done' for w in decision['writes']):
                    break
                time.sleep(.2)
            else:
                raise RuntimeError('Evaluation outbox was not delivered')
            results.append({'label': label, 'run_id': run['run_id'], 'decision_id': run['decision_id'],
                'engine': engine, 'median_seconds': median, 'reward': value, 'evaluation': receipt,
                'evidence': artifact, 'memory': decision['experience'],
                'receipts': [json.loads(w['receipt_json']) for w in decision['writes']]})
            print(json.dumps({'label': label, 'verified_reward': value, 'median_seconds': median,
                              'memory_revision': decision['experience']['revision']}), flush=True)
    (ROOT / 'output/evaluations.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
