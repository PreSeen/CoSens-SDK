"""Publish only a quality-confirmed local Recipe and execute it on the source task."""
import json
from pathlib import Path
import httpx
from dotenv import dotenv_values
from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor,ExecutionBudget
from cosens_rsi.demo_refund import RefundDriver,grade,FAMILY
from cosens_rsi.service import RSIService
from cosens_rsi.codex_trace import import_trace

ROOT=Path(__file__).resolve().parent;PROJECT=ROOT.parents[1]
source=json.loads((ROOT/'source/manifest.json').read_text());cfg=dotenv_values(PROJECT/'var/local.env')
with httpx.Client(base_url='http://127.0.0.1:18104',timeout=40,trust_env=False) as c:
 def api(method,path,body=None,admin=False,key=None):
  headers={'Authorization':'Bearer '+cfg['COSENS_RSI_ADMIN_TOKEN' if admin else 'COSENS_RSI_TOKEN']}
  if key:headers['Idempotency-Key']=key
  return c.request(method,path,json=body,headers=headers).raise_for_status().json()
 job=api('GET','/v1/improvements/'+source['job']['job_id'])
 decision=(job.get('report') or {}).get('release_decision',{})
 assert job['status'] in {'validated','published'} and decision.get('status')=='accepted' and decision.get('acceptance_path')=='quality','No independently confirmed quality gain; never force publication'
 if job['status']=='validated':
  release=api('POST',f"/internal/admin/v1/improvements/{job['job_id']}/publish",{'expected_revision':1,
    'reason':'用户授权的本地演示任务族；train-only 候选经不同 screen 与 4 个 confirm 工单达到质量门槛，无关键业务回归。'},admin=True)
  (ROOT/'release-receipt.json').write_text(json.dumps(release,ensure_ascii=False,indent=2)+'\n')
 active=api('GET','/v1/recipes/active?task_family='+FAMILY)
 assert active['bundle_hash']=='sha256:'+decision['candidate_sha256']
 (ROOT/'recipe-v2.json').write_text(json.dumps(active['bundle'],ensure_ascii=False,indent=2)+'\n')
 (ROOT/'final-improvement.json').write_text(json.dumps(job,ensure_ascii=False,indent=2)+'\n')
 work=ROOT/'after';work.mkdir(exist_ok=True)
 if (work/'manifest.json').exists():
  print('Preserving existing after attempt:',(work/'manifest.json').read_text());raise SystemExit
 case=json.loads((ROOT/'cases.private.json').read_text())[0]
 run=api('POST','/v1/runs/prepare',{'objective':case['task'],'task_family':FAMILY,'execution_mode':'guided','requires_evidence':True,
   'environment':{'case_id':FAMILY+'-qualified-adapter','case_title':'中文退款审核：Recipe 版本迭代与质量对照','executor_model':'gpt-6-luna','experiment_design':'recipe-evolution-quality'}},key='refund-recipe-v2-actual-delivery')
 rid=run['run_id'];(work/'manifest.json').write_text(json.dumps({'run_id':rid,'recipe_id':active['recipe_id'],'state':'running'}))
 step=api('POST',f'/v1/runs/{rid}/steps/next')
 for name,text in case['inputs'].items():(work/name).write_text(text)
 execution=HierarchicalExecutor().run(active['bundle'],case['task'],work,RefundDriver(work),ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),evidence_context={'snapshot':case['snapshot']})
 (work/'executor-result.json').write_text(json.dumps(execution,ensure_ascii=False,indent=2))
 assert execution['status']=='completed',execution['status']
 quality=grade(work,case['tests']);(work/'judgement.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
 assert quality['judge_valid']
 note='# 新版交付质量核验\n\n'+f"Recipe revision {active['revision']}，独立业务检查 {quality['passed']}/{quality['total']} 项通过。此处是原任务重放；独立质量结论以另 4 个确认工单的配对评测为准。\n\n"+'\n'.join(('✓ ' if q['passed'] else '✗ ')+q['title'] for q in quality['quality_checks'])+'\n'
 (work/'质量验收.md').write_text(note)
 refs=[]
 for name in execution['final']+['质量验收.md']:
  refs.append(api('POST','/v1/artifacts',{'name':name,'content':(work/name).read_text()}))
 api('POST',f"/v1/runs/{rid}/steps/{step['attempt_id']}/submit",{'attempt_token':step['attempt_token'],
    'outputs':{'files':[{'artifact_id':a['artifact_id'],'content_hash':a['content_hash']} for a in refs]}})
 api('POST',f'/v1/runs/{rid}/complete')
 service=RSIService(db_path=str(PROJECT/'var/cosens-rsi.sqlite3'),memory_token='trace-only-unused')
 trace=import_trace(service,work/'observable.jsonl',run_id=rid,node_id='review')
 result={'run_id':rid,'recipe_id':active['recipe_id'],'revision':active['revision'],'passed':quality['passed'],'total':quality['total'],'job_id':job['job_id'],'trace':trace}
 (work/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))
