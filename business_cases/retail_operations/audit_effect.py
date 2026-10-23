"""Independent post-run audit. Does not reward or modify any Run."""
import csv
import hashlib
import json
import math
import re
import sqlite3
import statistics
import subprocess
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parents[1]


def read(name):
    return json.loads((ROOT / name).read_text())


def main():
    for name, digest in read('input-lock.json').items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError('Frozen file changed: ' + name)
    protocol = read('protocol.json')
    first = read('output/stage1_runs.json')['baseline']
    last = read('output/post_run.json')
    before, after = read(first['measurement']), read(last['measurement'])
    oracle = read('data/oracle.json')['periods']
    for result in [before, after]:
        if result['period'] != protocol['held_out'] or result['kpis'] != oracle[protocol['held_out']]:
            raise RuntimeError('Held-out KPI mismatch')
        if len(result['trials']) != protocol['repeats'] or not all(t['exact_oracle_match'] for t in result['trials']):
            raise RuntimeError('Missing repeated correctness checks')
        if not math.isclose(statistics.median(t['seconds'] for t in result['trials']), result['median_seconds']):
            raise RuntimeError('Incorrect timing median')
        for key, file in [('engine_sha256', 'engine.py'), ('measure_sha256', 'measure.py'), ('protocol_sha256', 'protocol.json')]:
            if result[key] != hashlib.sha256((ROOT / file).read_bytes()).hexdigest():
                raise RuntimeError('Measurement code identity mismatch')
    with sqlite3.connect(f'file:{PROJECT / "var/cosens-rsi.sqlite3"}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        def saved(item):
            run = db.execute('SELECT * FROM runs WHERE id=?', (item['run_id'],)).fetchone()
            decision = db.execute('SELECT * FROM decisions WHERE id=? AND run_id=?', (item['decision_id'], item['run_id'])).fetchone()
            if not run or run['status'] != 'completed' or not decision or decision['actual_action_id'] != item['action_id']:
                raise RuntimeError('Incomplete Run or wrong actual action')
            if db.execute("SELECT COUNT(*) FROM step_attempts WHERE run_id=? AND status='passed'", (item['run_id'],)).fetchone()[0] != 3:
                raise RuntimeError('Guided workflow did not pass all three nodes')
            guidance = json.loads(decision['response_json'])
            if guidance['decision']['recommended_action_id'] != item['action_id']:
                raise RuntimeError('Business comparison task did not follow its recommendation')
            if guidance['candidates'] != protocol['actions']:
                raise RuntimeError('Candidate priors/identities changed')
            snapshot = json.loads(run['snapshot_json'])
            if snapshot['experience_context']['environment'] != protocol['environment']:
                raise RuntimeError('Execution environment differs')
            writes = [dict(w) for w in db.execute('SELECT state,receipt_json FROM memory_outbox WHERE decision_id=?', (item['decision_id'],))]
            if not writes or any(w['state'] != 'done' for w in writes):
                raise RuntimeError('Experience writeback unfinished')
            return {'run_id': item['run_id'], 'decision_id': item['decision_id'], 'actual_action': item['action_id'],
                    'recipe_id': run['recipe_id'], 'recipe_revision': run['recipe_revision'],
                    'experience_watermark': guidance['experience_watermark'], 'decision': guidance['decision'],
                    'memory_receipts': [json.loads(w['receipt_json']) for w in writes]}
        baseline, post = saved(first), saved(last)
        artifact_files = set()
        for ref in last.get('artifact_refs', []):
            file = ROOT / 'output' / Path(ref['name']).name
            if not file.exists():
                continue
            artifact = db.execute('SELECT content,content_hash FROM artifacts WHERE id=?', (ref['artifact_id'],)).fetchone()
            if not artifact or artifact['content_hash'] != ref['content_hash'] or artifact['content'] != file.read_text():
                raise RuntimeError('Final local file and RSI artifact differ: ' + file.name)
            artifact_files.add(file.name)
        expected_files = {'经营分析报告.md', '经营分析看板.html', 'RSI效果对照.md', 'monthly_kpis.csv', 'country_kpis.csv'}
        if not expected_files <= artifact_files:
            raise RuntimeError('Missing final business artifact references')
    if before['engine'] != first['action_id'] or after['engine'] != last['action_id']:
        raise RuntimeError('Recorded and measured actions differ')
    known_experiences = {item['memory']['experience_id'] for item in read('output/evaluations.json')}
    cited_experiences = {ref['experience_id'] for score in post['decision']['scores'] for ref in score['experience_refs']}
    if cited_experiences - known_experiences:
        raise RuntimeError('Uncontrolled experience influenced the comparison')
    if baseline['recipe_id'] != post['recipe_id'] or baseline['decision']['policy_version'] != post['decision']['policy_version']:
        raise RuntimeError('Recipe/policy differs across comparison')
    money_fields = {'gross_positive_gbp': 'gross_positive_micros', 'credit_abs_gbp': 'credit_abs_micros',
                    'net_gbp': 'net_micros', 'cancel_credit_gbp': 'cancel_credit_micros'}
    with (ROOT / 'output/monthly_kpis.csv').open(encoding='utf-8-sig', newline='') as file:
        rows = list(csv.DictReader(file))
    if len(rows) != len(oracle) or len({r['period'] for r in rows}) != len(oracle):
        raise RuntimeError('Missing or duplicated monthly CSV rows')
    for row in rows:
        expected = oracle[row['period']]
        for column, field in money_fields.items():
            if Decimal(row[column]) * 1000000 != expected[field]:
                raise RuntimeError('CSV money mismatch: ' + column)
        for field in ['raw_rows', 'excluded_rows', 'eligible_rows', 'positive_order_count', 'known_customer_count', 'repeat_customer_count']:
            if int(row[field]) != expected[field]:
                raise RuntimeError('CSV count mismatch: ' + field)
        ratio = expected['repeat_customer_count'] / expected['known_customer_count'] if expected['known_customer_count'] else 0
        if abs(float(row['repeat_customer_rate']) - ratio) > 1e-6:
            raise RuntimeError('CSV repeat-rate mismatch')
    with (ROOT / 'output/country_kpis.csv').open(encoding='utf-8-sig', newline='') as file:
        countries = list(csv.DictReader(file))
    expected_countries = oracle[protocol['held_out']]['countries']
    if len(countries) != len(expected_countries) or {r['country'] for r in countries} != set(expected_countries):
        raise RuntimeError('Country rows mismatch')
    for row in countries:
        if row['period'] != protocol['held_out']:
            raise RuntimeError('Country period differs')
        for column, field in list(money_fields.items())[:3]:
            if Decimal(row[column]) * 1000000 != expected_countries[row['country']][field]:
                raise RuntimeError('Country money mismatch')
    html = (ROOT / 'output/经营分析看板.html').read_text()
    for attributes, script in re.findall(r'<script([^>]*)>(.*?)</script>', html, re.S | re.I):
        if 'application/json' in attributes:
            json.loads(script)
        else:
            subprocess.run(['node', '--check', '-'], input=script, text=True, capture_output=True, check=True)
    seconds_before, seconds_after = before['median_seconds'], after['median_seconds']
    improvement = 1 - seconds_after / seconds_before
    build_seconds = read('data/index_manifest.json')['build_seconds']
    audit = {'schema': 'retail-rsi-effect-audit-v1', 'data_rows': sum(v['raw_rows'] for v in oracle.values()),
             'baseline': baseline, 'post_experience': post,
             'baseline_seconds': seconds_before, 'post_seconds': seconds_after,
             'baseline_trials': before['trials'], 'post_trials': after['trials'],
             'speedup_ratio': seconds_before / seconds_after, 'latency_reduction_fraction': improvement,
             'index_build_seconds': build_seconds,
             'approx_reports_to_amortize_build': math.ceil(build_seconds / (seconds_before - seconds_after)) if seconds_before > seconds_after else None,
             'recommendation_changed': baseline['decision']['recommended_action_id'] != post['decision']['recommended_action_id'],
             'quality_equal': before['kpis'] == after['kpis'], 'all_monthly_csv_exact': True, 'country_csv_exact': True,
             'html_scripts_parse': True, 'frozen_inputs_unchanged': True,
             'final_rsi_artifacts_match_local_files': sorted(artifact_files),
             'limits': ['Fixed neutral priors and predefined tools, not unrestricted Codex A/B.',
                        'Prebuilt verified index; build, hash checks, model/MCP overhead excluded from timing.',
                        'Three local repetitions; OS cache not purged or randomized; no significance claim.',
                        'No independent business-advice or user-acceptance score.',
                        'Candidate reranking effect, no automatic Recipe evolution/production promotion.']}
    (ROOT / 'output/effect_audit.json').write_text(json.dumps(audit, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: audit[k] for k in ['recommendation_changed', 'quality_equal', 'baseline_seconds', 'post_seconds',
                                           'speedup_ratio', 'latency_reduction_fraction', 'approx_reports_to_amortize_build']}))


if __name__ == '__main__':
    main()
