"""Readable quality evidence from administrator-attested code-repair evaluations."""
import hashlib
import json
import math


def quality_evidence(run):
    candidates = []
    for decision in run['decisions']:
        for evaluation in decision['evaluations']:
            artifact = run['artifacts'].get(evaluation.get('evidence_artifact_id'))
            if evaluation.get('status') != 'verified' or not artifact:
                continue
            if artifact['content_hash'] != evaluation.get('evidence_content_hash'):
                continue
            try:
                value = json.loads(artifact['content'])
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict) or value.get('schema') != 'code-repair-quality-v1':
                continue
            if value.get('run_id') != run['id'] or value.get('decision_id') != decision['id']:
                continue
            source = run['artifacts'].get(value.get('source_artifact_id'))
            if not source or source['content_hash'] != value.get('source_content_hash'):
                continue
            encoded = json.dumps(source['content'], ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
            if source['content_hash'] != 'sha256:'+hashlib.sha256(encoded).hexdigest():
                continue
            if value.get('action_id') != decision['actual'].get('actual_action_id'):
                continue
            checks = value.get('checks')
            if not isinstance(checks, list) or not checks or any(not isinstance(c, dict) or type(c.get('passed')) is not bool or not isinstance(c.get('key'), str) for c in checks):
                continue
            if len({c['key'] for c in checks}) != len(checks):
                continue
            passed = sum(c['passed'] for c in checks)
            reward = evaluation.get('value')
            if value.get('total') != len(checks) or value.get('passed') != passed or value.get('failed') != len(checks)-passed:
                continue
            if type(reward) not in (int, float) or not math.isclose(reward, 2*passed/len(checks)-1, abs_tol=1e-12):
                continue
            candidates.append((value, decision, artifact))
    return candidates[-1] if candidates else None


def quality_comparison(runs, route):
    trusted = set()
    for run in runs:
        for decision in run['decisions']:
            receipts = [json.loads(w['receipt_json']) for w in decision['writes'] if w['state'] == 'done' and w.get('receipt_json')]
            if receipts and receipts[-1].get('reward_status') == 'verified':
                trusted.add((decision['experience_id'], receipts[-1].get('revision')))
    records = [(r, quality_evidence(r)) for r in runs]
    for after, record in reversed(records):
        if not record:
            continue
        av, ad, _ = record
        decision = ad['guidance']['decision']
        if av.get('variant') != 'holdout' or decision.get('cold_start', True):
            continue
        refs = [ref for s in decision.get('scores', []) for ref in s.get('experience_refs', [])]
        if not refs or any((ref.get('experience_id'), ref.get('revision')) not in trusted for ref in refs):
            continue
        if decision.get('recommended_action_id') != av['action_id']:
            continue
        for before, brecord in records:
            if not brecord or before['created_at'] >= after['created_at']:
                continue
            bv, bd, _ = brecord
            if bv.get('variant') != 'holdout' or not bd['guidance']['decision'].get('cold_start', False):
                continue
            fields = ['initial_source_sha256', 'requirements_sha256', 'suite_sha256', 'protocol_sha256', 'existing_tests_sha256']
            same = all(bv.get(k) and bv[k] == av.get(k) for k in fields)
            same &= bd['guidance'].get('candidates') == ad['guidance'].get('candidates')
            same &= bool(bd['guidance'].get('contract_hash')) and bd['guidance']['contract_hash'] == ad['guidance'].get('contract_hash')
            same &= bd['guidance'].get('state', {}).get('observation') == ad['guidance'].get('state', {}).get('observation')
            same &= before['snapshot'].get('experience_context', {}).get('environment') == after['snapshot'].get('experience_context', {}).get('environment')
            same &= (before['recipe_id'], before['recipe_revision']) == (after['recipe_id'], after['recipe_revision'])
            same &= bd['guidance']['decision'].get('policy_version') == decision.get('policy_version')
            same &= bd['guidance']['decision'].get('recommended_action_id') == bv['action_id']
            same &= [c['key'] for c in bv['checks']] == [c['key'] for c in av['checks']]
            if not same:
                continue
            checks = [{'title': b['title'], 'before': b['passed'], 'after': a['passed'],
                       'before_detail': b.get('detail', ''), 'after_detail': a.get('detail', '')}
                      for b, a in zip(bv['checks'], av['checks'])]
            return {'available': True, 'kind': 'code_quality', 'before_run_id': before['id'], 'after_run_id': after['id'],
                    'before_route': route(bv['action_id']), 'after_route': route(av['action_id']),
                    'before_passed': bv['passed'], 'after_passed': av['passed'], 'total': av['total'],
                    'improved_checks': sum(not c['before'] and c['after'] for c in checks),
                    'regressed_checks': sum(c['before'] and not c['after'] for c in checks),
                    'recommendation_changed': bv['action_id'] != av['action_id'],
                    'experience_count': len(refs), 'checks': checks, 'scope': av.get('scope', '')}
    return None
