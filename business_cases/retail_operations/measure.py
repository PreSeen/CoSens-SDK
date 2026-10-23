"""Trusted measurement tool: fresh-process execution plus independent exact checks."""
import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--engine', required=True, choices=['raw_xlsx', 'indexed_sqlite'])
parser.add_argument('--period', required=True)
parser.add_argument('--out', required=True)
parser.add_argument('--repeats', type=int, default=3)
args = parser.parse_args()
if not 1 <= args.repeats <= 5:
    raise SystemExit('repeats must be 1..5')
oracle = json.loads((ROOT / 'data/oracle.json').read_text())
protocol = json.loads((ROOT / 'protocol.json').read_text())
manifest = json.loads((ROOT / 'data/index_manifest.json').read_text())
for path, expected in [('data/raw/online_retail.xlsx', manifest['source_sha256']),
                       ('data/ledger.sqlite3', manifest['index_sha256'])]:
    if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
        raise SystemExit('Input hash mismatch: ' + path)
trials = []
for index in range(args.repeats):
    begin = time.perf_counter()
    run = subprocess.run([sys.executable, str(ROOT / 'engine.py'), '--engine', args.engine,
                          '--period', args.period], capture_output=True, text=True, timeout=180, check=True)
    elapsed = time.perf_counter() - begin
    result = json.loads(run.stdout)
    exact = result == oracle['periods'][args.period]
    trials.append({'seconds': elapsed, 'exact_oracle_match': exact})
    print(json.dumps({'trial': index + 1, 'engine': args.engine, **trials[-1]}), flush=True)
    if not exact:
        raise SystemExit('Independent oracle mismatch')
median = statistics.median(t['seconds'] for t in trials)
value = 1 - 2 * min(median / protocol['reward']['latency_budget_seconds'], 1)
payload = {'schema': 'retail-measurement-v1', 'engine': args.engine, 'period': args.period,
           'source_sha256': manifest['source_sha256'], 'cache_sha256': manifest['index_sha256'],
           'engine_sha256': hashlib.sha256((ROOT / 'engine.py').read_bytes()).hexdigest(),
           'measure_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
           'oracle_sha256': hashlib.sha256((ROOT / 'data/oracle.json').read_bytes()).hexdigest(),
           'protocol_sha256': hashlib.sha256((ROOT / 'protocol.json').read_bytes()).hexdigest(),
           'trials': trials, 'median_seconds': median, 'reward': value, 'kpis': result,
           'quality_scope': 'Exact ledger/count reconciliation only; business advice is not independently scored.'}
output = ROOT / args.out
if not output.resolve().is_relative_to((ROOT / 'output').resolve()):
    raise SystemExit('Output must be under output/')
if output.exists():
    raise SystemExit('Refusing to overwrite measurement')
output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'saved': str(output), 'median_seconds': median, 'reward': value}))
