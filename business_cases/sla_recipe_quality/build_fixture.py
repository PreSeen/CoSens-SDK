import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parent
rules=(ROOT/'requirements.md').read_text();seed=(ROOT/'seed_sla.py').read_text()
specs=[
 ('train-lunch','train','售后中心午休跨窗误计时','2026-09-14','2026-10-01','2026-09-27'),
 ('train-calendar','train','企业客服调休和值班日历误判','2026-11-02','2026-11-09','2026-11-08'),
 ('screen-offset','screen','海外渠道UTC工单换算与午休','2026-07-06','2026-07-09','2026-07-12'),
 ('screen-seconds','screen','秒级流入的工单跨窗口截止','2026-08-03','2026-08-06','2026-08-09'),
 ('confirm-spring','confirm','春季客服休息日和秒级边界','2026-03-02','2026-03-05','2026-03-08'),
 ('confirm-afterhours','confirm','晚间提交与零分钟工单','2026-04-06','2026-04-09','2026-04-12'),
 ('confirm-multiday','confirm','长工时工单跨公司假期','2026-05-04','2026-05-07','2026-05-10'),
 ('confirm-interval','confirm','跨时区区间与日历无副作用','2026-06-01','2026-06-04','2026-06-07')]
cases=[]
for cid,part,title,monday,holiday,extra in specs:
 cal={'holidays':[holiday],'extra_workdays':[extra]};public={'scenario':title,'calendar':cal,'monday':monday,'holiday':holiday,'extra':extra,'fictional':True}
 task='修复中文客服 SLA 计算器：'+title+'。修改当前目录 sla.py，新增自测并交付中文修复说明。\n\n'+rules+'\n\n业务日历与上下文：\n'+json.dumps(public,ensure_ascii=False,indent=2)
 cases.append({'id':cid,'lineage_id':'sla-episode-'+cid,'partition':part,'task':task,
   'inputs':{'sla.py':seed,'公开要求.md':rules,'业务日历.json':json.dumps(public,ensure_ascii=False,indent=2)},
   'snapshot':{'schema':'sla-demo-public-input-v1','context':'frozen company calendar and public contract only','action_reranking':'off'},
   'tests':public})
for case in cases:case['editable_inputs']=['sla.py']
(ROOT/'cases.private.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2)+'\n')
bundle={'schema':'hierarchical-recipe-1','bindings':{'repair':'sla-repair@v1'},
 'steps':{'sla-repair@v1':{'id':'sla-repair','version':'v1','inputs':{'task':'text'},'outputs':{'files':'artifacts'},
  'body':{'preconditions':[],'steps':['阅读现有实现与全部公开要求，修复代码，编写自己的回归检查；交付可运行实现与中文说明，并自查接口及业务约束。'],
          'checks':['代码满足公开接口和要求，实际执行自己的测试，交付文件可读。'],'recovery':['依据真实错误进行修正并重新检查。']}}},
 'flow':{'id':'sla-code-delivery','version':'v1','checks':['保持公开接口，遵守全部公司工时规则，不硬编码案例答案。'],
  'nodes':[{'id':'repair','goal':'修复客服SLA工具并自测交付','skill':'repair','inputs':{'task':'$task'},'after':[],'max_repairs':1}],'final':'repair.files'}}
(ROOT/'recipe-v1.json').write_text(json.dumps(bundle,ensure_ascii=False,indent=2)+'\n')
protocol={'schema':'sla-recipe-quality-v1','family':'sla-recipe-quality-luna-v1','model':'gpt-6-luna','reasoning_effort':'medium',
 'partitions':{p:[c['id'] for c in cases if c['partition']==p] for p in ['train','screen','confirm']},
 'policy':{'max_rounds':1,'max_candidates':2,'min_confirm_tasks':4,'min_delta':0.03,'exploration_enabled':False},
 'train_rollouts':2,'design':'Same initial code and inputs per paired task. Model-generated Recipe from train-only observable code failures, screen selects, fresh confirm tests correctness. Keep all attempts. Synthetic calendars, no production claim.'}
protocol['editable_inputs']=['sla.py']
(ROOT/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
files=['requirements.md','seed_sla.py','reference.py','checker.py','cases.private.json','recipe-v1.json','protocol.json']
(ROOT/'input-lock.json').write_text(json.dumps({n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in files},indent=2)+'\n')
print('Frozen 8 SLA code repair tasks and 21 independent boundary groups per task')
