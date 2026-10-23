"""Build the reusable index; record its one-time cost separately."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

from engine import ROOT, raw_rows

started = time.perf_counter()
target = ROOT / 'data/ledger.sqlite3'
if target.exists():
    raise SystemExit('Index already exists; refusing to overwrite')
with sqlite3.connect(target) as db:
    db.execute('CREATE TABLE ledger(invoice TEXT,quantity INTEGER,month TEXT,price_micros INTEGER,customer TEXT,country TEXT)')
    db.executemany('INSERT INTO ledger VALUES(?,?,?,?,?,?)', raw_rows())
    db.execute('CREATE INDEX ledger_month ON ledger(month)')
    rows = db.execute('SELECT COUNT(*) FROM ledger').fetchone()[0]
elapsed = time.perf_counter() - started
manifest = {'schema': 'retail-index-v1', 'rows': rows, 'build_seconds': elapsed,
            'source_sha256': hashlib.sha256((ROOT / 'data/raw/online_retail.xlsx').read_bytes()).hexdigest(),
            'index_sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'bytes': target.stat().st_size}
(ROOT / 'data/index_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps(manifest))
