"""Execute the original Recipe through RSI, then explicitly request its learner."""
import json
from pathlib import Path
import time
import httpx
from dotenv import dotenv_values
from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor, ExecutionBudget
from cosens_rsi.demo_refund import RefundDriver, grade, FAMILY
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
      'reason':'User-authorized local Chinese refund quality Recipe evolution demo; honest neutral initial workflow, not production.'},admin=True)
  active=api('GET','/v1/recipes/active?task_family='+FAMILY)
 case=json.loads((ROOT/'cases.private.json').read_text())[0]
 run=api('POST','/v1/runs/prepare',{'objective':case['task'],'task_family':FAMILY,'execution_mode':'guided','requires_evidence':True,
     'environment':{'case_id':FAMILY+'-qualified-adapter','case_title':'中文退款审核：Recipe 版本迭代与质量对照','executor_model':'gpt-6-luna','experiment_design':'recipe-evolution-quality'}},key='refund-recipe-source-v2')
 rid=run['run_id'];(work/'manifest.json').write_text(json.dumps({'run_id':rid,'recipe_id':active['recipe_id'],'state':'running'},indent=2))
 step=api('POST',f'/v1/runs/{rid}/steps/next',{'data':{}})
 for name,text in case['inputs'].items():(work/name).write_text(text)
 result=HierarchicalExecutor().run(active['bundle'],case['task'],work,RefundDriver(work),ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),evidence_context={'snapshot':case['snapshot']})
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
 job=api('POST',f'/v1/runs/{rid}/improvements',{'data':{'reason':'用户明确要求 Recipe 本身从失败中迭代并提升质量；固定模型、工具、任务、证据与评估，仅从 train 生成方法候选，通过独立 screen/confirm 后才发布本地演示新版。'}})
 manifest={'run_id':rid,'recipe_id':active['recipe_id'],'source_passed':judgement['passed'],'source_total':judgement['total'],'job':job,'trace':imported}
 (work/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n');print(json.dumps(manifest,ensure_ascii=False))
