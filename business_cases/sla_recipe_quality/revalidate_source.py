"""Recover an authorized source edit rejected by the old read-only adapter.

No model rerun. Recheck the original delivered bytes under the corrected,
operator-owned editable-input contract; retain the old rejected execution.
"""
import json,shutil,tempfile,hashlib
from pathlib import Path
import httpx
from dotenv import dotenv_values
from cosens_rsi.algorithms.hierarchical import HierarchicalExecutor
from cosens_rsi.demo_sla import grade
from cosens_rsi.service import RSIService,canonical,now
from cosens_rsi.codex_trace import import_trace

ROOT=Path(__file__).resolve().parent;PROJECT=ROOT.parents[1];source=ROOT/'source'
meta=json.loads((source/'manifest.json').read_text());rid=meta['run_id']
case=json.loads((ROOT/'cases.private.json').read_text())[0];bundle=json.loads((ROOT/'recipe-v1.json').read_text())
for name in ['公开要求.md','业务日历.json']:
 assert (source/name).read_text()==case['inputs'][name]
quality=grade(source,case['tests']);assert quality['judge_valid'] and quality['task_success']
class SavedOutputs:
 def execute(self,instruction,*,timeout):
  for name in ['sla.py','修复说明.md','test_sla.py','执行记录.md']:shutil.copyfile(source/name,work/name)
  return {'completed':True,'outputs':{'files':['sla.py','修复说明.md','test_sla.py','执行记录.md']},
          'response':'Revalidated actual previously produced files; no new model execution.'}
with tempfile.TemporaryDirectory(prefix='sla-editable-contract-') as folder:
 work=Path(folder)
 for name,text in case['inputs'].items():(work/name).write_text(text)
 revalidation=HierarchicalExecutor().run(bundle,case['task'],work,SavedOutputs(),editable_inputs=['sla.py'])
assert revalidation['status']=='completed'
(source/'editable-contract-revalidation.json').write_text(json.dumps(revalidation,ensure_ascii=False,indent=2))
(source/'judgement.json').write_text(json.dumps(quality,ensure_ascii=False,indent=2))
text='# SLA代码交付与独立核验\n\n原始 GPT-6 Luna 交付通过21/21组独立业务检查。没有展示质量提升或生成新版Recipe。\n\n原编排器把目标源码当作只读输入，拒绝了用户要求的代码修改；该工程误判不算模型/Recipe质量失败。已通过显式 editable_inputs 重新校验原文件字节，未再次调用模型，其他公开资料仍保持不变。\n'
(source/'质量验收.md').write_text(text)
service=RSIService(db_path=str(PROJECT/'var/cosens-rsi.sqlite3'),memory_token='unused-trace-only')
with service._db() as db:
 pending=db.execute("SELECT id FROM step_attempts WHERE run_id=? AND status='pending'",(rid,)).fetchone()
 if pending:
  db.execute("UPDATE step_attempts SET status='rejected',error_json=? WHERE id=?",(canonical(['operator adapter wrongly classified editable source as immutable; recover saved valid output']),pending[0]))
  service._event(db,rid,'step_rejected',{'node_id':'repair','reason':'Editable-source adapter correction; actual output retained, no model rerun'})
cfg=dotenv_values(PROJECT/'var/local.env')
with httpx.Client(base_url='http://127.0.0.1:18104',headers={'Authorization':'Bearer '+cfg['COSENS_RSI_TOKEN']},timeout=30) as c:
 step=c.post(f'/v1/runs/{rid}/steps/next').raise_for_status().json();refs=[]
 for name in ['sla.py','修复说明.md','test_sla.py','执行记录.md','质量验收.md']:
  a=c.post('/v1/artifacts',json={'name':name,'content':(source/name).read_text()}).raise_for_status().json();refs.append({k:a[k] for k in ['artifact_id','content_hash']})
 c.post(f"/v1/runs/{rid}/steps/{step['attempt_id']}/submit",json={'attempt_token':step['attempt_token'],'outputs':{'files':refs}}).raise_for_status()
 c.post(f'/v1/runs/{rid}/complete').raise_for_status()
files=[source/'observable-first-attempt.jsonl',source/'observable.jsonl'];parts=[];seen=set()
for p in files:
 if p.exists():
  text=p.read_text();h=hashlib.sha256(text.encode()).hexdigest()
  if h not in seen:parts.append(text);seen.add(h)
(source/'observable-complete.jsonl').write_text('\n'.join(parts))
trace=import_trace(service,source/'observable-complete.jsonl',run_id=rid,node_id='repair')
meta.update(state='completed',passed=21,total=21,job={'status':'not_started','reason':'baseline already passes all independent groups'},
            adapter_revalidation=True,model_rerun=False,trace=trace)
(source/'manifest.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(meta,ensure_ascii=False))
