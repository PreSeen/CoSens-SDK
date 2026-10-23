"""Independent all-at-once dictionary checker. No imports from engine.py."""
import csv
import json
from collections import Counter,defaultdict
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def expected():
    wanted=set(json.loads((ROOT/'data/requests.json').read_text())['suppliers'])
    with (ROOT/'data/invoices.csv').open(newline='',encoding='utf-8') as f:
        book={r['invoice_id']:(r['supplier_id'],int(r['amount_cents'])) for r in csv.DictReader(f)}
    ledger={}; deliveries=Counter()
    with (ROOT/'data/payments.csv').open(newline='',encoding='utf-8') as f:
        for p in csv.DictReader(f):
            if p['status']=='成功' and p['supplier_id'] in wanted:
                key=(p['supplier_id'],p['invoice_id'],p['bank_ref'])
                deliveries[key]+=1
                ledger[key]=int(p['amount_cents'])
    amounts=defaultdict(int);invalid=defaultdict(int)
    for (supplier,invoice,_),amount in ledger.items():
        target=book.get(invoice)
        if target is None:
            invalid[(supplier,invoice,'缺失发票')]+=amount
        elif target[0]!=supplier:
            invalid[(supplier,invoice,'供应商错配')]+=amount
        else:
            amounts[(supplier,invoice)]+=amount
    results=[]
    for invoice,(supplier,billed) in book.items():
        if supplier not in wanted:
            continue
        settled=amounts[(supplier,invoice)]
        if settled==billed:
            continue
        issue='超付' if settled>billed else '未付款' if settled==0 else '少付'
        results.append(dict(supplier_id=supplier,invoice_id=invoice,issue=issue,expected_cents=billed,paid_cents=settled,difference_cents=settled-billed))
    results += [dict(supplier_id=s,invoice_id=i,issue=k,expected_cents=0,paid_cents=a,difference_cents=a) for (s,i,k),a in invalid.items()]
    results.sort(key=lambda r:(r['supplier_id'],r['invoice_id'],r['issue']))
    kpis={'supplier_count':len(wanted),'invoice_count':sum(s in wanted for s,_ in book.values()),
          'exception_count':len(results),'duplicate_delivery_count':sum(n-1 for n in deliveries.values()),
          'overpaid_cents':sum(max(r['difference_cents'],0) for r in results if r['issue']=='超付'),
          'underpaid_cents':sum(-r['difference_cents'] for r in results if r['issue'] in {'未付款','少付'}),
          'missing_invoice_cents':sum(v for (s,i,k),v in invalid.items() if k=='缺失发票'),
          'supplier_mismatch_cents':sum(v for (s,i,k),v in invalid.items() if k=='供应商错配')}
    return results,kpis
