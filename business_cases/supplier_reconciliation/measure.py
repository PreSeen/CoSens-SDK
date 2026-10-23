"""Measure three new processes, including index setup; compare exact outputs."""
import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from oracle import expected

ROOT=Path(__file__).resolve().parent


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--route',required=True);parser.add_argument('--output-dir',required=True)
    args=parser.parse_args();out=Path(args.output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    target=out/'measurement.json'
    if target.exists():raise SystemExit('Measurement exists; never overwrite an attempted result')
    lock=json.loads((ROOT/'input-lock.json').read_text())
    assert all(sha(ROOT/name)==digest for name,digest in lock.items()),'Frozen inputs changed'
    expected_rows,kpis=expected();trials=[];wall=time.perf_counter()
    for i in range(3):
        csv_path=out/f'exceptions-trial-{i+1}.csv'
        process=subprocess.run([sys.executable,str(ROOT/'engine.py'),'--route',args.route,'--output',str(csv_path)],capture_output=True,text=True,check=True,timeout=120)
        result=json.loads(process.stdout)
        with csv_path.open(newline='',encoding='utf-8') as f:
            rows=list(csv.DictReader(f))
        for row in rows:
            for field in ['expected_cents','paid_cents','difference_cents']:row[field]=int(row[field])
        result['exact_oracle_match']=rows==expected_rows and result['kpis']==kpis
        trials.append(result)
    m={'schema':'supplier-reconciliation-performance-v1','engine':args.route,'period':'2026-09',
       'median_seconds':statistics.median(t['elapsed_seconds'] for t in trials),
       'index_build_median_seconds':statistics.median(t['index_build_seconds'] for t in trials),
       'tool_wall_seconds':time.perf_counter()-wall,'trials':trials,'kpis':kpis,'sla_seconds':2.0,
       'source_sha256':hashlib.sha256(json.dumps({k:v for k,v in lock.items() if k.startswith('data/')},sort_keys=True).encode()).hexdigest(),
       'cache_sha256':'no-cache-fresh-database-every-trial','engine_sha256':lock['engine.py'],
       'measure_sha256':lock['measure.py'],'oracle_sha256':lock['oracle.py'],'protocol_sha256':lock['protocol.json'],
       'timing_scope':'invoice/payment CSV processing, cold database import and indexes when used, reconciliation and exception CSV output; excludes model/MCP and independent validation'}
    target.write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
    (out/'异常清单.csv').write_bytes((out/'exceptions-trial-1.csv').read_bytes())
    print(json.dumps(m,ensure_ascii=False))
    if not all(t['exact_oracle_match'] for t in trials):raise SystemExit(1)


if __name__=='__main__':main()
