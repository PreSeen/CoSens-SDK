"""Two ordinary access paths with identical public reconciliation rules."""
import argparse
import csv
import json
import sqlite3
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIELDS = ['supplier_id','invoice_id','issue','expected_cents','paid_cents','difference_cents']


def read_csv(name):
    with (ROOT/'data'/name).open(encoding='utf-8', newline='') as f:
        yield from csv.DictReader(f)


def reconcile(supplier, invoices, payments, owners):
    invoices = {r['invoice_id']:int(r['amount_cents']) for r in invoices}
    totals, extras, seen = {}, {}, set()
    duplicates = 0
    for p in payments:
        if p['status'] != '成功':
            continue
        key = (p['invoice_id'],p['bank_ref'])
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        invoice, amount = p['invoice_id'], int(p['amount_cents'])
        issue = '缺失发票' if invoice not in owners else '供应商错配' if owners[invoice] != supplier else None
        if issue:
            extras[(invoice,issue)] = extras.get((invoice,issue),0)+amount
        else:
            totals[invoice] = totals.get(invoice,0)+amount
    rows = []
    def add(invoice, issue, expected, paid):
        rows.append(dict(supplier_id=supplier,invoice_id=invoice,issue=issue,expected_cents=expected,paid_cents=paid,difference_cents=paid-expected))
    for invoice, expected in invoices.items():
        paid = totals.get(invoice,0)
        if paid != expected:
            add(invoice,'未付款' if paid==0 else '少付' if paid<expected else '超付',expected,paid)
    for (invoice,issue), amount in extras.items():
        add(invoice,issue,0,amount)
    return rows, len(invoices), duplicates


def execute(route, output):
    requests=json.loads((ROOT/'data/requests.json').read_text())
    started=time.perf_counter(); setup=0.0
    rows=[]; invoice_count=0; duplicates=0
    if route=='supplier_csv_scan':
        for supplier in requests['suppliers']:
            source=list(read_csv('invoices.csv'))
            owners={r['invoice_id']:r['supplier_id'] for r in source}
            selected=[r for r in source if r['supplier_id']==supplier]
            payments=[r for r in read_csv('payments.csv') if r['supplier_id']==supplier]
            found,count,repeats=reconcile(supplier,selected,payments,owners)
            rows.extend(found); invoice_count+=count; duplicates+=repeats
    elif route=='supplier_sqlite_batch':
        # Fresh database each invocation: setup and indexes are part of timing.
        with tempfile.TemporaryDirectory(prefix='supplier-cold-index-') as folder:
            con=sqlite3.connect(str(Path(folder)/'accounting.sqlite3'))
            con.execute('CREATE TABLE invoices(supplier_id TEXT,invoice_id TEXT PRIMARY KEY,amount_cents INTEGER)')
            con.execute('CREATE TABLE payments(supplier_id TEXT,invoice_id TEXT,bank_ref TEXT,amount_cents INTEGER,status TEXT)')
            con.executemany('INSERT INTO invoices VALUES(:supplier_id,:invoice_id,:amount_cents)',read_csv('invoices.csv'))
            con.executemany('INSERT INTO payments VALUES(:supplier_id,:invoice_id,:bank_ref,:amount_cents,:status)',read_csv('payments.csv'))
            con.execute('CREATE INDEX inv_supplier ON invoices(supplier_id)')
            con.execute('CREATE INDEX pay_supplier ON payments(supplier_id)')
            con.commit(); setup=time.perf_counter()-started
            con.row_factory=sqlite3.Row
            owners={r[0]:r[1] for r in con.execute('SELECT invoice_id,supplier_id FROM invoices')}
            for supplier in requests['suppliers']:
                found,count,repeats=reconcile(supplier,con.execute('SELECT * FROM invoices WHERE supplier_id=?',(supplier,)),
                    con.execute('SELECT * FROM payments WHERE supplier_id=?',(supplier,)),owners)
                rows.extend(found); invoice_count+=count; duplicates+=repeats
            con.close()
    else:
        raise ValueError('unknown route')
    rows.sort(key=lambda r:(r['supplier_id'],r['invoice_id'],r['issue']))
    with Path(output).open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,FIELDS);writer.writeheader();writer.writerows(rows)
    elapsed=time.perf_counter()-started
    kpis={'supplier_count':len(requests['suppliers']),'invoice_count':invoice_count,'exception_count':len(rows),
          'duplicate_delivery_count':duplicates,'overpaid_cents':sum(r['difference_cents'] for r in rows if r['issue']=='超付'),
          'underpaid_cents':sum(-r['difference_cents'] for r in rows if r['issue'] in {'少付','未付款'}),
          'missing_invoice_cents':sum(r['paid_cents'] for r in rows if r['issue']=='缺失发票'),
          'supplier_mismatch_cents':sum(r['paid_cents'] for r in rows if r['issue']=='供应商错配')}
    return {'elapsed_seconds':elapsed,'index_build_seconds':setup,'kpis':kpis}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--route',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(execute(args.route,args.output),ensure_ascii=False))
