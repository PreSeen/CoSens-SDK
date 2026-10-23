"""Read-only projections of real Recipe train/screen/confirm experiments."""
import hashlib
import json
import sqlite3
from pathlib import Path
from statistics import mean

from .algorithms.hierarchical import fingerprint, freeze_bundle

SLOT_NAMES={'steps':'执行方法','preconditions':'前置条件','checks':'检查规则','recovery':'修复方法'}
STATE_NAMES={'queued':'等待优化','running':'正在训练、筛选与确认','validated':'质量验证通过，等待本地发布',
             'awaiting_input':'等待初始材料','awaiting_runner':'等待统一执行器','awaiting_judge':'等待可信验收标准',
             'awaiting_validation_tasks':'等待独立任务','inconclusive':'验证证据不足','rejected':'候选未通过验证',
             'published':'新版 Recipe 已发布','no_change':'没有通过质量门槛的改进','deferred':'证据不足，未发布','failed':'优化执行失败'}
REASONS={'uncertain transfer effect':'确认样本中的改善尚不稳定，未达到发布门槛',
         'independent confirm gate passed':'独立质量确认通过','critical task regression':'出现关键业务退化，拒绝发布',
         'effect below preregistered minimum':'质量改善小于预设门槛','no screen survivor':'候选未通过独立筛选',
         'no_learnable_failure':'未观察到可学习的缺陷'}


def read_json(path):
    try:return json.loads(path.read_text())
    except (OSError,ValueError):return None


class RecipeObservability:
    def _recipe_audit_bundles(self,job):
        path=Path(self.db_path).resolve().parent/'harness-experiments'/job['id']/'hierarchical.sqlite3'
        if not path.is_file():return {}
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute('SELECT sha,data FROM bundles').fetchall()
        result={}
        for sha,data in rows:
            bundle=json.loads(data)
            if freeze_bundle(bundle)['sha256']==sha:result[sha]=bundle
        return result

    def get_case_candidate_recipe(self,case_id,job_id,principal):
        job=self._case_job(case_id,principal,job_id)
        report=json.loads(job['report_json']) if job.get('report_json') else {}
        rounds=report.get('result',{}).get('rounds',[])
        decision=report.get('release_decision') or (rounds[-1].get('decision',{}) if rounds else {})
        sha=decision.get('candidate_sha256')
        if not sha or not any(c['sha']==sha for r in rounds for c in r.get('candidates',[])):
            raise LookupError('no evaluated candidate Recipe')
        path=Path(self.db_path).resolve().parent/'harness-experiments'/job['id']/'hierarchical.sqlite3'
        if not path.is_file():raise LookupError('candidate registry missing')
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            row=db.execute('SELECT data FROM bundles WHERE sha=?',(sha,)).fetchone()
        if not row:raise LookupError('candidate Recipe missing')
        bundle=json.loads(row[0])
        if freeze_bundle(bundle)['sha256']!=sha:raise LookupError('candidate integrity mismatch')
        with self._db() as db:
            base=db.execute('SELECT family,revision FROM recipes WHERE id=?',(job['base_recipe_id'],)).fetchone()
        source=self.get_run(job['resource_id'],principal)
        return {'recipe_id':'candidate:'+sha,'task_family':base['family'] if base else source['task_package']['task_family'],
                'version':f"candidate-after-r{base['revision']}" if base else 'candidate-from-private-seed',
                'base_recipe_id':job['base_recipe_id'] or None,'base_revision':base['revision'] if base else None,'status':job['state'],
                'published':job['state']=='published','decision':decision.get('status'),'reason':decision.get('reason'),
                'bundle_hash':'sha256:'+sha,'bundle':bundle}

    def _case_job(self,case_id,principal,job_id=None):
        rows=self._case_rows(principal,case_id)
        if not rows:raise LookupError('case not found')
        ids={r['id'] for r in rows}
        with self._db() as db:
            records=db.execute('SELECT j.*,h.report_json,h.base_recipe_id,h.error FROM jobs j JOIN harness_jobs h ON h.job_id=j.id ORDER BY j.created_at DESC').fetchall()
        item=next((r for r in records if r['resource_id'] in ids and (job_id is None or r['id']==job_id)),None)
        if item is None:
            if job_id:raise LookupError('case improvement not found')
            return None
        return dict(item)

    def _evaluation_records(self,job):
        report=json.loads(job['report_json']) if job.get('report_json') else {}
        manifest=report.get('manifest',{})
        manifests={m.get('sha256'):m for m in report.get('manifest_generations',[])+[manifest]}
        root=Path(self.db_path).resolve().parent/'harness-experiments'/job['id']/'runs'
        records={}
        for file in root.glob('eval_*/record.json'):
            entry=read_json(file)
            if not entry:continue
            r=entry.get('record',{});generation=manifests.get(r.get('session_sha256'),{})
            case=next((c for c in generation.get('cases',[]) if c['id']==r.get('task_id')),None)
            if (not case or r.get('evaluation_id')!=file.parent.name
                or any(r.get(field)!=case.get(field) for field in ['task_sha256','snapshot_sha256','tests_sha256'])):continue
            records[r['evaluation_id']]={'record':r,'judgement':entry.get('judgement',{}),'folder':file.parent}
        return report,records

    def _evaluation_files(self,job_id,entry):
        result=[]
        for artifact in entry['record'].get('artifacts',[]):
            aid='harness:'+job_id+':'+entry['record']['evaluation_id']+':'+artifact['sha256']
            result.append({'artifact_id':aid,'name':artifact['path'],'kind':'markdown' if artifact['path'].endswith('.md') else 'record',
                           'summary':'该次真实执行保存的交付物','auxiliary':artifact['path']=='执行记录.md'})
        return result

    def _evaluation_trace(self,job,entry,bundle):
        """Project persisted evaluation facts without inventing a production Run."""
        r=entry['record'];files=self._evaluation_files(job['id'],entry)
        stages=[{'id':'task','title':'确认工单与冻结条件','status':'passed',
            'summary':'同一工单分别使用原始与候选 Recipe；执行器、快照和验收规则保持固定。',
            'inputs':[{'label':'公开任务','text':r.get('task_prompt','')}],
            'outputs':[{'label':'本次 Recipe','text':'冻结版本 '+r['bundle_sha256'][:12]}]}]
        for i,node in enumerate(r.get('trace',[])):
            step=bundle.get('steps',{}).get(node.get('step_ref'),{}).get('body',{})
            method='\n'.join(str(v) for v in step.get('steps',[]))
            output_paths={v for values in node.get('outputs',{}).values() if isinstance(values,list)
                          for v in values if isinstance(v,str)}
            node_files=[f for f in files if f['name'] in output_paths]
            outputs=([{'label':'节点交付物','artifacts':node_files}] if node_files else [])
            outputs.extend({'label':key,'text':str(value)} for key,value in node.get('outputs',{}).items()
                           if isinstance(value,(str,int,float,bool)))
            stages.append({'id':f'node:{i}','title':f"执行节点 {node.get('node','')} · 第 {node.get('attempt',0)+1} 次尝试",
                'status':'passed' if node.get('passed') else 'failed',
                'summary':'展示冻结 Recipe 方法及节点实际保存的交付物；完整操作过程可打开执行记录。节点检查通过不等于业务验收通过。',
                'inputs':[{'label':'执行方法','text':method or '未保存可读方法'},
                          {'label':'输入记录范围','text':'工单正文见第一步；审计记录保存了节点输入指纹，未保存完整解析后的节点输入。'}],
                'outputs':outputs,
                'errors':[str(v) for v in node.get('errors',[])]})
        checks=r.get('quality_checks',[]);passed=sum(c.get('passed') is True for c in checks)
        stages.append({'id':'acceptance','title':'独立业务验收','status':'passed' if r.get('task_success') else 'failed',
            'summary':f'{passed}/{len(checks)} 项业务检查通过。与另一版本执行同一组固定检查。',
            'inputs':[{'label':'实际交付物','artifacts':files}],
            'outputs':[{'label':'逐项结果','text':'\n'.join(('通过' if c.get('passed') else '未通过')+' · '+c['title'] for c in checks)}]})
        return {'trace_id':r['evaluation_id'],'evaluation_id':r['evaluation_id'],'source':'harness_evaluation',
                'bundle_hash':r['bundle_sha256'],'status':'completed' if r.get('execution_status')=='completed' else r.get('execution_status','unknown'),
                'quality':{'passed':passed,'total':len(checks)},'stages':stages,
                'trace_note':'这是隔离评测中的真实执行，不是正式任务 Run。此处不重复调用 Memory 或补造经验写回',
                'timeline':[]}

    def recipe_evolution_view(self,case_id,principal):
        job=self._case_job(case_id,principal)
        if not job:return None
        report,records=self._evaluation_records(job)
        with self._db() as db:
            base=db.execute('SELECT * FROM recipes WHERE id=?',(job['base_recipe_id'],)).fetchone()
            released=db.execute('SELECT r.* FROM recipes r JOIN recipe_releases x ON x.recipe_id=r.id WHERE x.job_id=? ORDER BY r.revision DESC LIMIT 1',(job['id'],)).fetchone()
            prior=db.execute('SELECT j.id,j.state,h.error FROM jobs j JOIN harness_jobs h ON h.job_id=j.id WHERE j.resource_id=? AND j.id<>? ORDER BY j.created_at',
                             (job['resource_id'],job['id'])).fetchall()
        manifest=report.get('manifest',{});partitions=manifest.get('partitions',{})
        progress=[]
        for partition,label in [('train','训练与失败诊断'),('screen','候选 Recipe 筛选'),('confirm','新工单独立确认')]:
            seen=[r for r in records.values() if r['record']['task_id'] in partitions.get(partition,[])]
            progress.append({'id':partition,'label':label,'task_count':len(partitions.get(partition,[])),
                             'executions':len(seen),'failed_checks':sum(sum(c.get('passed') is False for c in r['record'].get('quality_checks',[])) for r in seen)})
        result=report.get('result',{});rounds=result.get('rounds',[]);decision=report.get('release_decision') or (rounds[-1].get('decision',{}) if rounds else {})
        comparison=decision.get('comparison',{})
        valid=comparison.get('coverage')==1 and bool(comparison.get('pairs'))
        base_bundle=json.loads(base['bundle_json']) if base else {}
        changes=[]
        for round_ in rounds:
            for c in round_.get('candidates',[]):
                change=c['change'];target=change.get('target');old=base_bundle.get('bindings',{}).get(target)
                previous=base_bundle.get('steps',{}).get(old,{}).get('body',{}).get(change.get('slot'),[])
                changes.append({'target':target or change.get('node'),'slot':SLOT_NAMES.get(change.get('slot'),change.get('op')),
                    'before':previous,'after':change.get('value') if change.get('layer')=='step' else ['流程结构变更'],
                    'selected':c['sha']==decision.get('candidate_sha256'),'screen_delta':c.get('screen',{}).get('mean_delta')})
        bundles={report.get('base_sha256'):base_bundle}
        registry=Path(self.db_path).resolve().parent/'harness-experiments'/job['id']/'hierarchical.sqlite3'
        if registry.is_file() and decision.get('candidate_sha256'):
            with sqlite3.connect(registry.as_uri()+'?mode=ro',uri=True) as db:
                row=db.execute('SELECT data FROM bundles WHERE sha=?',(decision['candidate_sha256'],)).fetchone()
            if row:
                bundle=json.loads(row[0])
                if freeze_bundle(bundle)['sha256']==decision['candidate_sha256']:bundles[decision['candidate_sha256']]=bundle
        pairs=[]
        for pair in comparison.get('pairs',[]) if valid else []:
            before=records.get(pair.get('base_evaluation_id'));after=records.get(pair.get('candidate_evaluation_id'))
            if not before or not after:valid=False;break
            br,ar=before['record'],after['record']
            if br['bundle_sha256']!=report.get('base_sha256') or ar['bundle_sha256']!=decision.get('candidate_sha256'):
                valid=False;break
            if br['score']!=pair['base'] or ar['score']!=pair['candidate']:valid=False;break
            bc={c['key']:c for c in br.get('quality_checks',[])};ac={c['key']:c for c in ar.get('quality_checks',[])}
            checks=[{'title':q['title'],'before':q['passed'],'after':ac.get(key,{}).get('passed',False),'critical':q.get('critical',False)} for key,q in bc.items()]
            pairs.append({'task_id':pair['task_id'],'title':br.get('task_prompt','').split('\n',1)[0].split('。',1)[0],
                'input_text':br.get('task_prompt',''),'before_score':pair['base'],'after_score':pair['candidate'],
                'before_passed':sum(c['before'] for c in checks),'after_passed':sum(c['after'] for c in checks),'total':len(checks),
                'checks':checks,'before_files':self._evaluation_files(job['id'],before),'after_files':self._evaluation_files(job['id'],after),
                'before_trace':self._evaluation_trace(job,before,bundles.get(br['bundle_sha256'],{})),
                'after_trace':self._evaluation_trace(job,after,bundles.get(ar['bundle_sha256'],{}))})
        previous=[]
        for attempt in prior:
            error=attempt['error'] or ''
            reason='候选结构校验未通过，未计入质量结论。' if error=='ValueError' else '措辞检查误判，评分已作废，未计入质量结论。' if error.startswith('InvalidLanguageCheck') else STATE_NAMES.get(attempt['state'],attempt['state'])
            previous.append({'job_id':attempt['id'],'status':attempt['state'],'reason':reason})
        reason=decision.get('reason') or result.get('termination_reason') or ('工件路径适配预检失败，该次业务评分无效，已排除。' if (job.get('error') or '').startswith('ArtifactDiscoveryAdapterError') else job.get('error'))
        return {'job_id':job['id'],'status':job['state'],'status_text':STATE_NAMES.get(job['state'],job['state']),
            'preparation':report.get('preparation'),'evidence_level':report.get('evidence_level'),
            'previous_attempts':previous,
            'before_recipe_id':base['id'] if base else None,'before_revision':base['revision'] if base else None,
            'after_recipe_id':released['id'] if released else None,'after_revision':released['revision'] if released else None,
            'base_hash':report.get('base_sha256'),'candidate_hash':decision.get('candidate_sha256'),
            'diagnosis':rounds[-1].get('diagnosis',{}).get('reason') if rounds else None,
            'progress':progress,'changes':changes,'available':valid,'pairs':pairs if valid else [],
            'before_score':mean(p['before_score'] for p in pairs) if valid else None,
            'after_score':mean(p['after_score'] for p in pairs) if valid else None,
            'delta':comparison.get('mean_delta') if valid else None,'decision':decision.get('status'),
            'improved':comparison.get('improved'),'tied':comparison.get('tied'),'regressed':comparison.get('regressed'),
            'reason':REASONS.get(reason,reason),
            'acceptance_path':decision.get('acceptance_path'),'scope':'固定执行器、工具、预算、逐题快照和独立规则评估，仅 Recipe 改变。候选来自训练工单失败；筛选与确认使用不同工单。确认工单为演示小样本，不代表生产泛化或统计确定性。'}

    def get_case_harness_artifact(self,case_id,artifact_id,principal):
        parts=artifact_id.split(':')
        if len(parts)!=4 or parts[0]!='harness':raise LookupError('harness artifact not found')
        _,job_id,evaluation_id,digest=parts
        job=self._case_job(case_id,principal,job_id);report,records=self._evaluation_records(job)
        # Only evaluations bound by the final server report can be downloaded.
        allowed=set()
        for r in report.get('result',{}).get('rounds',[]):
            decision=r.get('decision',{})
            for p in decision.get('comparison',{}).get('pairs',[]):
                allowed.update([p.get('base_evaluation_id'),p.get('candidate_evaluation_id')])
        manifest=report.get('manifest',{});parts=manifest.get('partitions',{})
        train_screen=set(parts.get('train',[])+parts.get('screen',[]))
        bundles={report.get('base_sha256')}|{c['sha'] for r in report.get('result',{}).get('rounds',[]) for c in r.get('candidates',[])}
        if evaluation_id in records:
            record=records[evaluation_id]['record']
            if record['task_id'] in train_screen and record['bundle_sha256'] in bundles:allowed.add(evaluation_id)
        if evaluation_id not in allowed or evaluation_id not in records:raise LookupError('evaluation not visible in case report')
        entry=records[evaluation_id]
        item=next((a for a in entry['record'].get('artifacts',[]) if a['sha256']==digest),None)
        if not item:raise LookupError('evaluation artifact not found')
        root=(entry['folder']/'artifacts').resolve();path=(root/item['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise LookupError('evaluation artifact missing')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=digest:raise LookupError('evaluation artifact changed')
        card=self._artifact_card({'id':artifact_id,'name':item['path'],'content':data.decode(),'content_hash':'sha256:'+digest})
        if card['kind'] in {'markdown','text'}:card['content']=data.decode()
        elif item['path'].endswith('.json'):
            actual=json.loads(data)
            if isinstance(actual,dict) and 'policy_id' in actual:
                def money(key):
                    value=actual.get(key)
                    return f'¥{value/100:.2f}' if type(value) is int else '未保存有效金额'
                card['highlights']=[{'label':'采用政策','value':actual.get('policy_id','未记录')},
                    {'label':'本次退款','value':money('cash_total_cents')},
                    {'label':'运费退款','value':money('shipping_refund_cents')}]
                notes=actual.get('uncertainties',[])
                card['notes']=[n for n in [actual.get('customer_reply',''),*(notes if isinstance(notes,list) else [])] if isinstance(n,str)]
        return card
