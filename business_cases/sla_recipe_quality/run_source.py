"""Execute the original Recipe through RSI, then explicitly request its learner."""
import json
from pathlib import Path
import time
import httpx
from dotenv import dotenv_values
from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor, ExecutionBudget
from cosens_rsi.demo_sla import SlaDriver, grade, FAMILY
from cosens_rsi.service import RSIService
from cosens_rsi.codex_trace import import_trace

ROOT=Path(__file__).resolve().parent
PROJECT=ROOT.parents[1]
work=ROOT/'source';work.mkdir(exist_ok=True)
cfg=dotenv_values(PROJECT/'var/local.env')
with httpx.Client(base_url='http://127.0.0.1:18104',timeout=40,trust_env=False) as c:
 def api(method,path,body=None,admin=False,key=None):
  h={'Authorization':'Bearer '+cfg['COSENS_RSI_ADMIN_TOKEN' if admin else 'COSENS_RSI_TOKEN']}
  if key:h['Idempotency-Key']=key
  return c.request(method,path,json=body,headers=h).raise_for_status().json()
 if (work/'manifest.json').exists():
  print('Source already exists, preserve previous attempt:',(work/'manifest.json').read_text());raise SystemExit
 active=api('GET','/v1/recipes/active?task_family='+FAMILY)
 if active['status']=='not_found':
  api('POST','/internal/admin/v1/recipes/bootstrap-bundle',{'task_family':FAMILY,'bundle':json.loads((ROOT/'recipe-v1.json').read_text()),
      'reason':'User-authorized local Chinese SLA code-quality Recipe evolution demo; ordinary initial method, no production deployment.'},admin=True)
  active=api('GET','/v1/recipes/active?task_family='+FAMILY)
 case=json.loads((ROOT/'cases.private.json').read_text())[0]
 run=api('POST','/v1/runs/prepare',{'objective':case['task'],'task_family':FAMILY,'execution_mode':'guided','requires_evidence':True,
     'environment':{'case_id':FAMILY,'case_title':'中文客服SLA修复：Recipe 迭代与代码质量对照','executor_model':'gpt-6-luna','experiment_design':'recipe-evolution-quality'}},key='sla-recipe-source-v1')
 rid=run['run_id'];(work/'manifest.json').write_text(json.dumps({'run_id':rid,'recipe_id':active['recipe_id'],'state':'running'},indent=2))
 step=api('POST',f'/v1/runs/{rid}/steps/next',{'data':{}})
 for name,text in case['inputs'].items():(work/name).write_text(text)
 result=HierarchicalExecutor().run(active['bundle'],case['task'],work,SlaDriver(work),ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),evidence_context={'snapshot':case['snapshot']},editable_inputs=case.get('editable_inputs',[]))
 (work/'executor-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 if result['status']!='completed':raise RuntimeError('Source execution did not complete: '+result['status'])
 judgement=grade(work,case['tests']);(work/'judgement.json').write_text(json.dumps(judgement,ensure_ascii=False,indent=2))
 text='# 首次交付质量验收\n\n'+f"独立业务检查：{judgement['passed']}/{judgement['total']} 项通过。\n\n"+'\n'.join(('✓ ' if q['passed'] else '✗ ')+q['title'] for q in judgement['quality_checks'])+'\n'
 (work/'质量验收.md').write_text(text)
 refs=[]
 for name in result['final']+['质量验收.md']:
  refs.append(api('POST','/v1/artifacts',{'name':name,'content':(work/name).read_text()}))
 api('POST',f"/v1/runs/{rid}/steps/{step['attempt_id']}/submit",{'attempt_token':step['attempt_token'],'outputs':{'files':[{'artifact_id':a['artifact_id'],'content_hash':a['content_hash']} for a in refs]}})
 api('POST',f'/v1/runs/{rid}/complete',{'data':{}})
 service=RSIService(db_path=str(PROJECT/'var/cosens-rsi.sqlite3'),memory_token='unused-trace-import')
 imported=import_trace(service,work/'observable.jsonl',run_id=rid,node_id='review')
 job=({'status':'no_learning_needed','reason':'source already passes all independent business groups'} if judgement['task_success'] else api('POST',f'/v1/runs/{rid}/improvements',{'data':{'reason':'用户要求真实 Recipe 迭代以提高中文客服SLA代码修复质量，固定模型工具预算与输入；仅训练失败生成候选，独立screen与4个confirm评估正确性；保留所有结果。'}}))
 manifest={'run_id':rid,'recipe_id':active['recipe_id'],'source_passed':judgement['passed'],'source_total':judgement['total'],'job':job,'trace':imported}
 (work/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');print(json.dumps(manifest,ensure_ascii=False))
