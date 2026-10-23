"""Independent result verification and RSI feedback. Never used by task executor."""
import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
import httpx
from dotenv import dotenv_values
from oracle import expected

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('label',choices=['baseline','after']);args=parser.parse_args()
    work=ROOT/'work'/args.label
    receipt_path=work/'evaluation-receipt.json'
    if receipt_path.exists():
        print(receipt_path.read_text());return
    lock=json.loads((ROOT/'input-lock.json').read_text())
    assert all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==h for name,h in lock.items())
    meta=json.loads((work/'run.json').read_text());m=json.loads((work/'measurement.json').read_text())
    expected_rows,kpis=expected();assert m['kpis']==kpis
    trials=m['trials'];assert len(trials)==3
    for i,t in enumerate(trials):
        assert t['exact_oracle_match'] is True and math.isfinite(t['elapsed_seconds']) and t['elapsed_seconds']>0
        assert 0<=t['index_build_seconds']<=t['elapsed_seconds']
        with (work/f'exceptions-trial-{i+1}.csv').open(encoding='utf-8',newline='') as f: rows=list(csv.DictReader(f))
        for row in rows:
            for field in ['expected_cents','paid_cents','difference_cents']:row[field]=int(row[field])
        assert rows==expected_rows
    median=statistics.median(t['elapsed_seconds'] for t in trials)
    assert m['median_seconds']==median and m['engine']==meta['action_id']
    reward=max(-1,min(1,(2-median)/2))
    cfg=dotenv_values(PROJECT/'var/local.env')
    with httpx.Client(base_url='http://127.0.0.1:18104',timeout=30,trust_env=False) as client:
        def api(method,path,body=None,admin=False):
            token=cfg['COSENS_RSI_ADMIN_TOKEN' if admin else 'COSENS_RSI_TOKEN']
            return client.request(method,path,json=body,headers={'Authorization':'Bearer '+token}).raise_for_status().json()
        run=api('GET','/v1/runs/'+meta['run_id']);assert run['status']=='completed'
        assert meta['measurement_artifact']['artifact_id'] in json.dumps(run['result'])
        # Verify the actual committed artifact as well as local files.
        import sqlite3
        with sqlite3.connect(PROJECT/'var/cosens-rsi.sqlite3') as db:
            stored=db.execute('SELECT content,content_hash FROM artifacts WHERE id=? AND principal=?',
                              (meta['measurement_artifact']['artifact_id'],'local-codex')).fetchone()
        assert stored and stored[1]==meta['measurement_artifact']['content_hash'] and json.loads(stored[0])==m
        path=f"/v1/runs/{meta['run_id']}/decisions/{meta['decision_id']}"
        decision=api('GET',path);assert decision['actual']['actual_action_id']==meta['action_id']
        evidence={'schema':'supplier-operator-evaluation-v1','run_id':meta['run_id'],'decision_id':meta['decision_id'],
                  'measurement_artifact':meta['measurement_artifact'],'exact_rows':len(expected_rows),'exact_kpis':kpis,
                  'median_seconds':median,'includes_cold_index_build':True,'sla_seconds':2,'reward':reward,
                  'checks':{'input_lock_unchanged':True,'committed_measurement_matches_disk':True,'all_three_csv_rows_exact':True,'all_kpis_exact':True},
                  'scope':'Independent dictionary checker, source lock and committed artifacts. Same-task online feedback replay; no hidden holdout or end-to-end model latency claim.'}
        artifact=api('POST','/v1/artifacts',{'name':args.label+'-独立对账验收.json','content':json.dumps(evidence,ensure_ascii=False,indent=2)})
        evaluation=api('POST',path.replace('/v1/runs/','/internal/admin/v1/runs/')+'/evaluations',
                       {'evaluation_id':'supplier-eval-'+meta['decision_id'],'reward_spec':'verified-decision-return-v1',
                        'status':'verified','value':reward,'evidence_artifact_id':artifact['artifact_id'],'evidence_content_hash':artifact['content_hash']},admin=True)
        for _ in range(100):
            decision=api('GET',path)
            receipts=[json.loads(w['receipt_json']) for w in decision['writes'] if w['state']=='done' and w.get('receipt_json')]
            if receipts and receipts[-1].get('reward_status')=='verified':break
            time.sleep(.2)
        else:raise RuntimeError('Verified feedback has not reached Memory')
        result={'label':args.label,'reward':reward,'median_seconds':median,'exception_count':len(expected_rows),
                'evaluation':evaluation,'receipts':receipts,'evidence_artifact':artifact}
        receipt_path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
