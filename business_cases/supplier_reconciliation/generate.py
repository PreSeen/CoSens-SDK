"""Deterministic fictional accounts; creates source data only, not expected answers."""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT/'data'
DATA.mkdir(exist_ok=True)

def write(path, fields, records):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(records)

invoices, payments = [], []
for i in range(80000):
    supplier = f'S{i%48:03}'
    invoice = f'INV{i:07}'
    amount = 10000 + (i*137)%100000
    invoices.append(dict(supplier_id=supplier, invoice_id=invoice, amount_cents=amount))
    if i%211 == 0:
        continue
    paid = amount - 300 if i%173 == 0 else amount + 500 if i%149 == 0 else amount
    ps = f'S{(i+1)%48:03}' if i%283 == 0 else supplier
    payment = dict(supplier_id=ps, invoice_id=invoice, bank_ref=f'BANK{i:07}', amount_cents=paid, status='成功')
    payments.append(payment)
    if i%97 == 0:
        payments.append(dict(payment))
    if i%397 == 0:
        payments.append(dict(supplier_id=supplier, invoice_id=invoice, bank_ref=f'EXTRA{i:07}', amount_cents=1200, status='成功'))
    if i%101 == 0:
        payments.append(dict(supplier_id=supplier, invoice_id=invoice, bank_ref=f'FAILED{i:07}', amount_cents=amount, status='失败'))
for i in range(200):
    payments.append(dict(supplier_id=f'S{i%48:03}', invoice_id=f'MISSING{i:05}', bank_ref=f'ORPHAN{i:05}', amount_cents=20000+i*19, status='成功'))
write(DATA/'invoices.csv', ['supplier_id','invoice_id','amount_cents'], invoices)
write(DATA/'payments.csv', ['supplier_id','invoice_id','bank_ref','amount_cents','status'], payments)
(DATA/'requests.json').write_text(json.dumps({'period':'2026-09','suppliers':[f'S{i:03}' for i in range(18)],'fictional':True},ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'invoice_rows':len(invoices),'payment_rows':len(payments),'supplier_requests':18}))
