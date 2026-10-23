"""Independent oracle: read OOXML cells directly, without the engine/openpyxl."""
import hashlib
import json
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
results, orders, customers = {}, defaultdict(set), defaultdict(lambda: defaultdict(set))
country_totals = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
source = ROOT / 'data/raw/online_retail.xlsx'
with zipfile.ZipFile(source) as archive:
    root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
    strings = [''.join(t.text or '' for t in si.iter(NS + 't')) for si in root]
    del root
    with archive.open('xl/worksheets/sheet1.xml') as stream:
        parsed = ET.iterparse(stream, events=('start', 'end'))
        _, doc = next(parsed)
        for event, row in parsed:
            if event != 'end' or row.tag != NS + 'row':
                continue
            if row.get('r') == '1':
                row.clear()
                continue
            cells = {}
            for cell in row:
                v = cell.find(NS + 'v')
                text = v.text if v is not None else ''
                cells[''.join(c for c in cell.get('r') if c.isalpha())] = strings[int(text)] if cell.get('t') == 's' and text else text
            invoice = cells.get('A', '')
            month = (datetime(1899, 12, 30) + timedelta(days=float(cells['E']))).strftime('%Y-%m')
            if month not in results:
                results[month] = dict(period=month, raw_rows=0, excluded_rows=0, eligible_rows=0,
                    gross_positive_micros=0, credit_abs_micros=0, net_micros=0,
                    cancel_credit_micros=0, noncancel_negative_micros=0)
            result = results[month]
            result['raw_rows'] += 1
            quantity, price = int(Decimal(cells['D'])), int((Decimal(cells['F']) * 1000000).to_integral_value())
            if price <= 0 or quantity == 0:
                result['excluded_rows'] += 1
            else:
                result['eligible_rows'] += 1
                amount = quantity * price
                result['net_micros'] += amount
                country = cells.get('H', '')
                country_totals[month][country][0] += amount
                if amount > 0:
                    result['gross_positive_micros'] += amount
                    country_totals[month][country][1] += amount
                    if not invoice.upper().startswith('C'):
                        orders[month].add(invoice)
                        customer = cells.get('G', '')
                        if customer:
                            customers[month][str(int(Decimal(customer)))].add(invoice)
                else:
                    result['credit_abs_micros'] -= amount
                    country_totals[month][country][2] -= amount
                    kind = 'cancel_credit_micros' if invoice.upper().startswith('C') else 'noncancel_negative_micros'
                    result[kind] -= amount
            row.clear()
            # Clear completed row placeholders as well; the oracle is streaming.
            for parent in list(doc):
                if parent.tag == NS + 'sheetData':
                    parent.clear()
for month, result in results.items():
    result.update(positive_order_count=len(orders[month]), known_customer_count=len(customers[month]),
        repeat_customer_count=sum(len(v) >= 2 for v in customers[month].values()),
        countries={k: dict(zip(('net_micros', 'gross_positive_micros', 'credit_abs_micros'), v))
                   for k, v in sorted(country_totals[month].items())})
payload = {'schema': 'retail-oracle-v1', 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
           'implementation': 'stdlib OOXML streaming parser; separate from engine code', 'periods': results}
(ROOT / 'data/oracle.json').write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + '\n')
print(json.dumps({'oracle_periods': list(sorted(results)), 'rows': sum(v['raw_rows'] for v in results.values())}))
