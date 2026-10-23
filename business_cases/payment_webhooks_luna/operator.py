"""Local operator evaluation; never run by the Codex task worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx


CASE = Path(__file__).resolve().parent
PROJECT = CASE.parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('label', choices=['baseline', 'training_a', 'training_b', 'after'])
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    lock = json.loads((CASE/'input-lock.json').read_text())
    for name, digest in lock.items():
        assert hashlib.sha256((CASE/name).read_bytes()).hexdigest() == digest, 'Frozen input changed: '+name
    manifest = json.loads((CASE/'work'/args.label/'run.json').read_text())
    cfg = dict(line.split('=', 1) for line in (PROJECT/'var/local.env').read_text().splitlines() if '=' in line)
    ordinary = {'Authorization': 'Bearer '+cfg['COSENS_RSI_TOKEN']}
    admin = {'Authorization': 'Bearer '+cfg['COSENS_RSI_ADMIN_TOKEN']}
    with httpx.Client(base_url='http://127.0.0.1:18104', trust_env=False, timeout=30) as client:
        def api(method, path, body=None, privileged=False):
            response = client.request(method, path, json=body, headers=admin if privileged else ordinary)
            response.raise_for_status()
            return response.json()

        run = api('GET', '/v1/runs/'+manifest['run_id'])
        decision_path = '/v1/runs/'+manifest['run_id']+'/decisions/'+manifest['decision_id']
        decision = api('GET', decision_path)
        assert run['status'] == 'completed'
        assert decision['actual']['actual_action_id'] == manifest['action_id']
        source_ref = manifest['source_artifact']
        assert source_ref['artifact_id'] in json.dumps(run['result']), 'Source absent from final submission'
        with sqlite3.connect(PROJECT/'var/cosens-rsi.sqlite3') as db:
            row = db.execute('SELECT content,content_hash FROM artifacts WHERE id=? AND principal=?',
                             (source_ref['artifact_id'], 'local-codex')).fetchone()
        assert row and row[1] == source_ref['content_hash']
        source = row[0]
        assert source == (CASE/'work'/args.label/'payment.py').read_text(), 'Submitted and on-disk source differ'
        assert (CASE/'work'/args.label/'test_payment.py').read_bytes() == (CASE/'seed_test_payment.py').read_bytes()
        variant = args.label if args.label.startswith('training_') else 'holdout'
        with tempfile.TemporaryDirectory(prefix='payment-acceptance-') as folder:
            candidate = Path(folder)/'payment.py'
            candidate.write_text(source)
            # Credentials are never inherited by the independently executed patch.
            process = subprocess.run([sys.executable, '-I', str(CASE/'acceptance.py'), str(candidate), '--variant', variant],
                                     cwd=folder, env={'PATH': os.defpath, 'PYTHONIOENCODING': 'utf-8'},
                                     capture_output=True, text=True, timeout=30)
            if process.returncode:
                raise RuntimeError('Independent checker crashed: '+process.stderr[-1000:])
            outcome = json.loads(process.stdout)
        evidence = {'label': args.label, 'variant': variant,
            'run_id': manifest['run_id'], 'decision_id': manifest['decision_id'], 'action_id': manifest['action_id'],
            'source_artifact_id': source_ref['artifact_id'], 'source_content_hash': row[1],
            'initial_source_sha256': lock['seed_payment.py'], 'requirements_sha256': lock['requirements.md'],
            'suite_sha256': lock['acceptance.py'], 'protocol_sha256': lock['protocol.json'],
            'existing_tests_sha256': lock['seed_test_payment.py'],
            **outcome, 'schema': 'code-repair-quality-v1',
            'scope': '业务仿真代码的固定候选开发方式对照；真实 Codex 补丁与独立回放。仅衡量这份公开契约和固定验收集，不代表真实支付系统上线验证或通用能力提升。'}
        path = PROJECT/'var'/f'{CASE.name}-{args.label}-independent.json'
        if path.exists():
            assert json.loads(path.read_text()) == evidence, 'Previously saved evaluation changed'
        else:
            path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+'\n')
            path.chmod(0o600)
        if args.publish:
            if args.label in {'baseline', 'after'}:
                assert (CASE/'work/after/run.json').exists(), 'Never release holdout evidence before post execution is frozen'
                post = json.loads((CASE/'work/after/run.json').read_text())
                assert api('GET', '/v1/runs/'+post['run_id'])['status'] == 'completed'
            artifact = api('POST', '/v1/artifacts', {'name': args.label+'-独立业务验收.json', 'content': json.dumps(evidence, ensure_ascii=False, sort_keys=True)})
            # Reuse an identical artifact on a repeat invocation to keep evaluation idempotent.
            receipt_path = PROJECT/'var'/f'{CASE.name}-{args.label}-published.json'
            if receipt_path.exists():
                prior = json.loads(receipt_path.read_text())
                artifact = prior['artifact']
            evaluation = api('POST', decision_path.replace('/v1/runs/', '/internal/admin/v1/runs/')+'/evaluations',
                {'evaluation_id': 'payment-quality-'+manifest['decision_id'], 'reward_spec': 'verified-decision-return-v1',
                 'status': 'verified', 'value': outcome['reward'], 'evidence_artifact_id': artifact['artifact_id'],
                 'evidence_content_hash': artifact['content_hash']}, privileged=True)
            for _ in range(100):
                current = api('GET', decision_path)
                if current['writes'] and all(w['state'] == 'done' for w in current['writes']):
                    receipts = [json.loads(w['receipt_json']) for w in current['writes']]
                    if receipts[-1].get('reward_status') == 'verified':
                        break
                time.sleep(.2)
            else:
                raise RuntimeError('Verified evaluation did not reach Memory')
            receipt_path.write_text(json.dumps({'artifact': artifact, 'evaluation': evaluation, 'receipts': receipts}, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'label': args.label, 'passed': outcome['passed'], 'total': outcome['total'],
                          'reward': outcome['reward'], 'published': args.publish,
                          'failures': [r['title'] for r in outcome['checks'] if not r['passed']]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
