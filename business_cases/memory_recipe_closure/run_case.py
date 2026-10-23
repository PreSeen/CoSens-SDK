"""Real local RSI lifecycle. The actor is GPT-6 Luna; RSI owns all Memory I/O."""
from copy import deepcopy
import json,os,time
from pathlib import Path
import httpx
from dotenv import dotenv_values
from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor,ExecutionBudget,fingerprint
from cosens_rsi.demo_refund import RefundDriver,grade
from cosens_rsi.service import RSIService
from cosens_rsi.codex_trace import import_trace

ROOT=Path(__file__).resolve().parent;PROJECT=ROOT.parents[1]
FAMILY='memory-recipe-closure-luna-v1';TITLE='中文家居退款审核：Memory 与 Recipe 迭代完整闭环'
cfg=dotenv_values(PROJECT/'var/local.env')
for k,v in cfg.items():
 if v is not None:os.environ[k]=v
client=httpx.Client(base_url='http://127.0.0.1:18104',timeout=45,trust_env=False)
def api(method,path,body=None,admin=False,key=None):
 headers={'Authorization':'Bearer '+cfg['COSENS_RSI_ADMIN_TOKEN' if admin else 'COSENS_RSI_TOKEN']}
 if key:headers['Idempotency-Key']=key
 return client.request(method,path,json=body,headers=headers).raise_for_status().json()


def execute(label,case):
 work=ROOT/label;work.mkdir(exist_ok=True)
 if (work/'manifest.json').exists():raise RuntimeError('Preserve existing real execution; do not rerun silently')
 run=api('POST','/v1/runs/prepare',{'objective':case['task'],'task_family':FAMILY,'execution_mode':'guided','requires_evidence':True,
   'task_lineage_id':case['lineage_id'],
   'environment':{'case_id':FAMILY,'case_title':TITLE,'executor_model':'gpt-6-luna','experiment_design':'memory-recipe-closure','closure_role':label}},key=FAMILY+':'+label)
 rid=run['run_id'];(work/'manifest.json').write_text(json.dumps({'run_id':rid,'state':'running','recipe_id':run['recipe']['recipe_id']},indent=2))
 step=api('POST',f'/v1/runs/{rid}/steps/next',{})
 for name,text in case['inputs'].items():(work/name).write_text(text)
 result=HierarchicalExecutor().run(run['recipe']['bundle'],case['task'],work,RefundDriver(work),
     ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),
     evidence_context={'snapshot':deepcopy(case['snapshot']),'memory_context':step['memory_context']})
 (work/'executor-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
 if result['status']!='completed':raise RuntimeError('Real source execution did not complete')
 judgement=grade(work,case['tests']);(work/'judgement.json').write_text(json.dumps(judgement,ensure_ascii=False,indent=2))
 refs=[]
 for name in result['final']:
  a=api('POST','/v1/artifacts',{'name':name,'content':(work/name).read_text()});refs.append({'artifact_id':a['artifact_id'],'content_hash':a['content_hash']})
 api('POST',f"/v1/runs/{rid}/steps/{step['attempt_id']}/submit",{'attempt_token':step['attempt_token'],'outputs':{'files':refs}})
 api('POST',f'/v1/runs/{rid}/complete',{})
 r={**judgement,'evaluation_id':'real-'+rid,'task_id':case['id'],'task_lineage_id':case['lineage_id'],
    'task_sha256':fingerprint({'task':case['task'],'inputs':case['inputs']}),'bundle_sha256':result['bundle_sha256'],
    'protocol_sha256':fingerprint(json.loads((ROOT/'protocol.json').read_text())),'session_sha256':FAMILY,
    'task_prompt':case['task'],'trace':result['trace'],'execution_status':result['status'],'artifacts':[]}
 artifact=api('POST','/v1/artifacts',{'name':'独立质量验收.json','content':json.dumps(r,ensure_ascii=False)})
 api('POST',f'/internal/admin/v1/runs/{rid}/recipe-evaluation',{'record':r,'artifact_id':artifact['artifact_id'],'content_hash':artifact['content_hash']},admin=True)
 service=RSIService(str(PROJECT/'var/cosens-rsi.sqlite3'))
 imported=import_trace(service,work/'observable.jsonl',run_id=rid,node_id='review')
 manifest={'run_id':rid,'recipe_id':run['recipe']['recipe_id'],'recipe_revision':run['recipe']['revision'],
           'passed':judgement['passed'],'total':judgement['total'],'trace':imported,
           'memory_recalled':len(step['memory_context'].get('recipe_memory',{}).get('items',[]))}
 (work/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'stage':label,**manifest},ensure_ascii=False),flush=True)
 return rid


if __name__=='__main__':
 import sys
 mode=sys.argv[1] if len(sys.argv)>1 else 'before'
 case=json.loads((ROOT/'source.private.json').read_text())
 if mode=='before':
  active=api('GET','/v1/recipes/active?task_family='+FAMILY)
  if active['status']=='not_found':
   api('POST','/internal/admin/v1/recipes/bootstrap-bundle',{'task_family':FAMILY,'bundle':json.loads((ROOT/'recipe-v1.json').read_text()),
        'reason':'User-authorized fresh local demo; normal initial method, independent frozen eight-task confirmation.'},admin=True)
  rid=execute('before',case)
  job=api('POST',f'/v1/runs/{rid}/improvements',{'data':{'reason':'用户要求Memory与Recipe统一闭环：以Memory中真实训练经验生成候选，固定条件独立筛选确认，验证后发布本地Recipe，随后检索经验完成原任务重放。'}})
  (ROOT/'job.json').write_text(json.dumps(job,indent=2)+'\n');print(json.dumps(job),flush=True)
 elif mode=='after':
  job=json.loads((ROOT/'job.json').read_text());current=api('GET','/v1/improvements/'+job['job_id'])
  if current['status']=='validated':
   published=api('POST','/internal/admin/v1/improvements/'+job['job_id']+'/publish',{'expected_revision':1,'reason':'用户授权本地完整闭环验证；固定Memory及独立确认通过后的Recipe应用。'},admin=True)
   (ROOT/'release.json').write_text(json.dumps(published,indent=2)+'\n')
  elif current['status']!='published':raise RuntimeError('Quality gate did not permit publication: '+current['status'])
  execute('after',case)
 else:raise ValueError(mode)
