"""Freeze public fictional cases and independently computed reference specs."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
RULES=(ROOT/'rules.md').read_text()


def case(cid,part,theme,items,coupon,approvals,history,*,paid_at='2026-09-12',fault=True,shipping=1299,shipping_history=0):
    # IDs, dates and evidence differ by business episode, not simply output labels.
    prefix=cid.upper()
    policies=[{'source_id':prefix+'-POL-OLD','policy_id':'after-sale-v1','effective_from':'2026-01-01','old_allow_partial_shipping':True},
              {'source_id':prefix+'-POL-NEW','policy_id':'after-sale-v2','effective_from':'2026-09-01','old_allow_partial_shipping':False}]
    lines=[dict(line_id=f'L{i+1}',quantity=q,unit_price_cents=p,coupon_eligible=eligible,name=n) for i,(q,p,eligible,n) in enumerate(items)]
    requested={l['line_id']:l['quantity'] for l in lines}
    docs={'scenario':theme,'fictional':True,'processing_date':'2026-10-23','policies':policies,
          'order':{'source_id':prefix+'-ORDER','paid_at':paid_at,'lines':lines,'coupon_cents':coupon,'shipping_paid_cents':shipping,'merchant_fault':fault},
          'request':{'source_id':prefix+'-REQUEST','requested_total_qty':requested,'customer_phone':'13800138000','customer_bank':'6222020202020202020'},
          'approval_records':[],'refund_ledger':{'source_id':prefix+'-LEDGER','events':[]},
          'invoice':{'source_id':prefix+'-INVOICE','display_cents':sum(l['quantity']*l['unit_price_cents'] for l in lines)+shipping},
          'service_note':{'source_id':prefix+'-CHAT','text':'客户希望全退且明天到账；客服便签按最新看到的政策估算，金额和审批须以正式记录为准。'}}
    for i,(line,qty,state,signed,day) in enumerate(approvals):
        docs['approval_records'].append({'source_id':prefix+f'-APP-{i+1}','line_id':line,'approved_total_qty':qty,'status':state,'signed_by':'审核员陈' if signed else '', 'signed_at':day})
    for i,(line,qty,amount,status,duplicate) in enumerate(history):
        event={'refund_id':prefix+f'-REF-{i+1}','line_id':line,'approved_total_qty':qty,'amount_cents':amount,'status':status}
        docs['refund_ledger']['events'].append(event)
        if duplicate:docs['refund_ledger']['events'].append(dict(event))
    if shipping_history:
        docs['refund_ledger']['events'].append({'refund_id':prefix+'-SHIP','line_id':'shipping','approved_total_qty':0,'amount_cents':shipping_history,'status':'已到账'})
    task='请审核这个仿真售后工单：'+theme+'。遵守全部公开契约，产出准确的退款金额、证据引用及可发送的中文回复。\n\n'+RULES+'\n\n资料（资料排列顺序不表示证据优先级）：\n'+json.dumps(docs,ensure_ascii=False,indent=2)
    return {'id':cid,'lineage_id':'refund-episode-'+cid,'partition':part,'task':task,'inputs':{'materials.json':json.dumps(docs,ensure_ascii=False,indent=2),'交付契约.md':RULES},'snapshot':{'schema':'refund-demo-frozen-context-v1','source':'public fictional order documents only','action_reranking':'off'},'tests':{'documents':docs,'schema':'refund-quality-reference-v1'}}

# Distinct episodes cover allocation, historical ledger, missing approval, policy dates and shipping.
cases=[
 case('train-allocation','train','多件商品部分退货，订单券分摊与重复到账流水',[(7,1999,True,'组合装'),(3,3997,True,'配件'),(1,0,False,'赠品')],2003,
      [('L1',5,'已批准',True,'2026-10-01'),('L2',1,'已批准',True,'2026-10-02')],[('L1',2,3197,'已到账',True),('L2',1,500,'处理中',False)]),
 case('train-approval','train','未签草稿不能当批准，旧新政策与整单退货边界',[(3,3333,True,'电器'),(2,2501,True,'耗材')],1777,
      [('L1',3,'已批准',True,'2026-09-20'),('L2',2,'已批准',False,'2026-10-20')],[('L1',1,2938,'已到账',False)],shipping=1599),
 case('screen-ledger','screen','跨批次审批提高累计件数，历史重试去重',[(9,1237,True,'零件'),(4,4571,True,'工具'),(1,890,False,'不参与活动品')],1733,
      [('L1',3,'已批准',True,'2026-09-20'),('L1',8,'已批准',True,'2026-10-12'),('L2',2,'已批准',True,'2026-10-13'),('L3',1,'已批准',True,'2026-10-13')],
      [('L1',3,3421,'已到账',True),('L2',1,4171,'已到账',False)]),
 case('screen-policy','screen','旧订单按当时政策部分退货返运费，不按处理日期套新政',[(5,2999,True,'旧订单主商品'),(2,1099,True,'配件')],777,
      [('L1',2,'已批准',True,'2026-09-10'),('L2',1,'已批准',True,'2026-09-12')],[],paid_at='2026-08-18',shipping=999,shipping_history=399),
 case('confirm-rounding','confirm','多件数小额尾差与不参加优惠商品分摊',[(11,1357,True,'小件组合'),(7,2461,True,'附加件'),(2,999,False,'非活动商品'),(1,0,False,'赠品')],2299,
      [('L1',7,'已批准',True,'2026-10-02'),('L2',4,'已批准',True,'2026-10-04'),('L3',1,'已批准',True,'2026-10-03')],[('L1',3,3751,'已到账',True),('L2',2,4567,'处理中',False)]),
 case('confirm-whole-order','confirm','质量原因整单退货，已退商品与已退运费避免重复支付',[(6,5671,True,'主机套装'),(3,1789,True,'连接件'),(1,0,False,'赠品')],3997,
      [('L1',6,'已批准',True,'2026-10-03'),('L2',3,'已批准',True,'2026-10-03')],[('L1',2,10001,'已到账',True),('L2',1,1501,'已到账',False)],shipping=1899,shipping_history=599),
 case('confirm-signed-scope','confirm','已批数量低于客户申请，晚到未签草稿与已签审批冲突',[(8,2027,True,'家电配件'),(5,3989,True,'耗材包')],2501,
      [('L1',3,'已批准',True,'2026-09-21'),('L1',8,'已批准',False,'2026-10-21'),('L2',2,'已批准',True,'2026-10-02')],
      [('L1',1,1781,'已到账',False),('L2',1,3631,'已到账',True)],fault=False,shipping=1199),
 case('confirm-old-policy','confirm','历史订单旧政策、已到账和处理中并存、发票含税展示不替代实付',[(7,4567,True,'套件'),(4,1239,True,'补充件'),(2,567,False,'特价品')],3127,
      [('L1',4,'已批准',True,'2026-09-01'),('L2',2,'已批准',True,'2026-09-02'),('L3',1,'已批准',True,'2026-09-03')],
      [('L1',1,4123,'已到账',True),('L1',4,9000,'处理中',False)],paid_at='2026-07-21',shipping=1399,shipping_history=0)
]
(ROOT/'cases.private.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
body={'preconditions':[],'steps':['阅读任务和全部授权资料，按公开契约完成退款核算、证据审核和中文回复；保存结果，并自查金额、审批边界及文件完整性。'],
      'checks':['输出覆盖用户要求，金额和证据有依据，保存的文件可读。'],'recovery':['发现明确缺项时进行修正并重新检查。']}
bundle={'schema':'hierarchical-recipe-1','steps':{'refund-review@v1':{'id':'refund-review','version':'v1','inputs':{'task':'text'},'outputs':{'files':'artifacts'},'body':body}},
        'bindings':{'review':'refund-review@v1'},'flow':{'id':'refund-review-flow','version':'v1','checks':['满足任务的全部公开要求，不编造审批或付款。'],
        'nodes':[{'id':'review','goal':'完成中文退款审核与自查，保存交付结果','skill':'review','inputs':{'task':'$task'},'after':[],'max_repairs':1}], 'final':'review.files'}}
(ROOT/'recipe-v1.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n')
protocol={'schema':'refund-recipe-quality-demo-v1','family':'refund-recipe-quality-luna-v1','model':'gpt-6-luna',
 'partitions':{p:[c['id'] for c in cases if c['partition']==p] for p in ['train','screen','confirm']},
 'policy':{'max_rounds':1,'max_candidates':2,'min_confirm_tasks':4,'min_delta':0.03,'exploration_enabled':False},
 'design':'Same models/tools/budgets, frozen per-task snapshots and independent integer/reference checks; only Recipe changes. Train-only model proposer, screen selection, fresh confirm; retain all failures. No candidate route reranking.'}
protocol['train_rollouts']=2
protocol['evaluation_revision']='v2-substantive-pending-evidence'
(ROOT/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
print('Frozen',len(cases),'cases, two train/two screen/four confirm; neutral initial one-node review Recipe')
