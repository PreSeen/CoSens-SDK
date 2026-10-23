"""Freeze a fresh Chinese order-audit cohort before any model execution."""
from copy import deepcopy
import hashlib,json,random
from pathlib import Path

ROOT=Path(__file__).resolve().parent
RULES=(ROOT.parent/'refund_recipe_quality/rules.md').read_text()
FAMILY='memory-recipe-closure-luna-v1'

def make_case(index,partition):
    rng=random.Random(2026102300+index)
    cid=f'closure-{partition}-{index:02d}';prefix=cid.upper()
    count=4+(index%3)
    lines=[];approvals=[];events=[]
    for i in range(count):
        key=f'L{i+1}';q=rng.randrange(6,18);p=rng.randrange(777,6500)
        lines.append({'line_id':key,'quantity':q,'unit_price_cents':p,'coupon_eligible':i!=count-1,'name':['家居套装','维修配件','连接模块','耗材组合','替换部件','包装材料'][i]})
        confirmed=q if index in (8,11) else rng.randrange(2,q)
        approvals.append({'source_id':f'{prefix}-APP-{i+1}','line_id':key,'approved_total_qty':confirmed,'status':'已批准','signed_by':'业务审核员林','signed_at':f'2026-10-{i+2:02d}'})
        if i%2==0:
            approvals.append({'source_id':f'{prefix}-DRAFT-{i+1}','line_id':key,'approved_total_qty':q,'status':'已批准','signed_by':'','signed_at':'2026-10-22'})
        event={'refund_id':f'{prefix}-R-{i+1}','line_id':key,'approved_total_qty':1,'amount_cents':rng.randrange(101,p//2+101),'status':'已到账'}
        events.append(event)
        if i%2==1:events.append(deepcopy(event))
        events.append({'refund_id':f'{prefix}-P-{i+1}','line_id':key,'approved_total_qty':confirmed,'amount_cents':rng.randrange(100,2000),'status':'处理中'})
    # One free gift exercises coverage without changing paid-item shipping eligibility.
    lines.append({'line_id':'GIFT','quantity':1,'unit_price_cents':0,'coupon_eligible':False,'name':'促销赠品'})
    policies=[{'source_id':prefix+'-POL-OLD','policy_id':'after-sale-v1','effective_from':'2026-01-01','old_allow_partial_shipping':True},
              {'source_id':prefix+'-POL-NEW','policy_id':'after-sale-v2','effective_from':'2026-09-01','old_allow_partial_shipping':False}]
    if index%2:policies.reverse()
    docs={'scenario':'家居配件分批退货：正式审批、未签草稿与历史退款核对 '+str(index),
          'fictional':True,'processing_date':'2026-10-23','policies':policies,
          'order':{'source_id':prefix+'-ORDER','paid_at':'2026-08-17' if index%3==0 else '2026-09-19',
                   'lines':lines,'coupon_cents':rng.randrange(1701,4299),'shipping_paid_cents':rng.randrange(799,1899),'merchant_fault':index%4!=1},
          'request':{'source_id':prefix+'-REQUEST','requested_total_qty':{l['line_id']:l['quantity'] for l in lines},
                     'customer_phone':'13900139000','customer_bank':'6228480000000000123'},
          'approval_records':approvals,'refund_ledger':{'source_id':prefix+'-LEDGER','events':events},
          'invoice':{'source_id':prefix+'-INVOICE','display_cents':sum(l['quantity']*l['unit_price_cents'] for l in lines)+999},
          'service_note':{'source_id':prefix+'-CHAT','text':'客户希望全部退货，客服曾口头估计运费全退；正式范围、到账金额和处理时间仍须核对。'}}
    task='请审核这个仿真售后工单：'+docs['scenario']+'。完成退款核算、证据冲突审核与中文客户回复。\n\n'+RULES+'\n\n资料：\n'+json.dumps(docs,ensure_ascii=False,indent=2)
    return {'id':cid,'lineage_id':'memory-recipe-business-episode-'+str(index),'partition':partition,'task':task,
            'inputs':{'materials.json':json.dumps(docs,ensure_ascii=False,indent=2),'交付契约.md':RULES},
            'snapshot':{'source':'frozen public fictional business documents','schema':'memory-recipe-case-v1'},
            'tests':{'schema':'refund-quality-reference-v1','documents':docs}}

if __name__=='__main__':
    if (ROOT/'input-lock.json').exists():raise SystemExit('Already frozen; preserve evidence')
    cases=[make_case(i,p) for p,ids in [('train',[1,2]),('screen',[3,4]),('confirm',range(5,13))] for i in ids]
    source=make_case(0,'train')
    (ROOT/'cases.private.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
    (ROOT/'source.private.json').write_text(json.dumps(source,ensure_ascii=False,indent=2)+'\n')
    recipe=json.loads((ROOT.parent/'refund_recipe_quality/recipe-v1.json').read_text())
    (ROOT/'recipe-v1.json').write_text(json.dumps(recipe,ensure_ascii=False,indent=2)+'\n')
    protocol={'family':FAMILY,'model':'gpt-6-luna','train_rollouts':2,
              'policy':{'max_rounds':1,'max_candidates':2,'min_confirm_tasks':8,'min_delta':.03,'exploration_enabled':False},
              'design':'Predeclared fresh cohort. Two train, two screen, eight paired confirm. Same frozen inputs/Memory/model/tools/budget per arm; only Recipe changes. No retries selected by scores; preserve every attempt.'}
    (ROOT/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
    names=['cases.private.json','source.private.json','recipe-v1.json','protocol.json']
    (ROOT/'input-lock.json').write_text(json.dumps({n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names},indent=2)+'\n')
    print('Frozen source, 2 train, 2 screen, 8 confirm tasks before all model calls')
