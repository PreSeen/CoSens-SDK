"""Readable four-phase trace backed by real Runs, evaluations and Memory receipts."""
import json


def io(label,text):return {'label':label,'text':text}


class LifecycleObservability:
    def recipe_lifecycle_view(self,case_id,principal,runs,views,evolution):
        if not evolution:return None
        job=self._case_job(case_id,principal,evolution['job_id'])
        with self._db() as db:
            writes=[dict(r) for r in db.execute('SELECT * FROM recipe_memory_outbox WHERE job_id=? ORDER BY created_at',(job['id'],))]
            reads=[dict(r) for r in db.execute('SELECT * FROM recipe_learning_views WHERE job_id=? ORDER BY created_at',(job['id'],))]
        if not writes and not reads:return None
        report,records=self._evaluation_records(job)
        source=next(v for v in views if v['run_id']==job['resource_id'])
        source={**source,'trace_id':source['run_id'],'role':'RSI 前 · 初始执行'}
        rows={r['evaluation_id']:r for r in writes}
        def receipt_line(row):
            receipt=json.loads(row['receipt_json']) if row['receipt_json'] else {}
            label={'train':'训练','screen':'筛选','confirm':'确认','production':'方法发布'}[row['partition_name']]
            state={'done':'已写回','queued':'等待写回','running':'正在写回','retry_wait':'等待重试','failed':'写回失败'}[row['state']]
            return f"{label} · {state} · {receipt.get('experience_id',row['evaluation_id'])} · 修订 {receipt.get('revision','未完成')}"
        partitions=report.get('manifest',{}).get('partitions',{})
        train=[r for r in writes if r['partition_name']=='train']
        validate=[r for r in writes if r['partition_name'] in {'screen','confirm'}]
        methods=[]
        for change in evolution['changes']:
            before='\n'.join(change['before'] or [])
            after='\n'.join(change['after']) if isinstance(change['after'],list) else str(change['after'])
            methods.append(f"{'筛选采用' if change['selected'] else '其他候选'} · {change['target']} / {change['slot']}\n原方法：\n{before}\n候选方法：\n{after}")
        learning_stages=[
            {'id':'training-memory','title':'真实训练执行写入 Memory','status':'passed' if train and all(r['state']=='done' for r in train) else 'pending',
             'summary':'每次训练执行保存实际输出、失败反馈及固定验收结果；不是模型自称成功。下方可选择单次执行查看完整过程。',
             'inputs':[io('训练工单',f"{len(partitions.get('train',[]))} 个工单，{len(train)} 次实际执行")],
             'outputs':[io('Memory 写回回执','\n'.join(receipt_line(r) for r in train) or '尚无已记录的写回')]},
            {'id':'learning-memory-read','title':'RSI 从 Memory 读取训练经验','status':'passed' if reads else 'pending',
             'summary':'优化器读取版本固定的训练经验；筛选和确认分区不会进入此学习视图。',
             'inputs':[io('经验来源','\n'.join(f"{len(json.loads(r['refs_json']))} 条训练经验 · Memory 水位 {r['watermark']}" for r in reads) or '尚未读取训练经验')],
             'outputs':[io('训练视图','\n'.join('版本指纹 '+r['view_hash'][:12] for r in reads) or '等待 Memory 训练资料持久化')]},
            {'id':'recipe-changes','title':'诊断问题并生成候选 Recipe','status':'passed' if methods else 'pending',
             'summary':evolution.get('diagnosis') or '根据真实训练失败生成可复用方法，不读取确认题答案。',
             'inputs':[io('学习依据',f"{len(reads)} 次已持久化的 Memory 训练视图读取")],
             'outputs':[io('实际方法变更','\n\n'.join(methods) or '尚无已保存的候选变更')]}]
        validation_stages=[
            {'id':'fixed-context','title':'冻结 Memory 与对照条件','status':'passed',
             'summary':'独立确认用于验证 Recipe 改变的效果；同一工单的两个版本共享冻结的输入、记忆上下文、执行器、工具和预算。',
             'inputs':[io('验证安排',f"筛选 {len(partitions.get('screen',[]))} 个工单；确认 {len(partitions.get('confirm',[]))} 个工单")],
             'outputs':[io('冻结实验','会话版本 '+report.get('manifest',{}).get('sha256','未记录')[:12])]},
            {'id':'independent-comparison','title':'候选筛选与独立质量确认','status':'passed' if evolution['available'] else 'pending',
             'summary':f"改善 {evolution.get('improved') or 0} 份，持平 {evolution.get('tied') or 0} 份，退化 {evolution.get('regressed') or 0} 份。",
             'inputs':[io('候选方法',f"{len(evolution['changes'])} 个实际候选")],
             'outputs':[io('确认工单结果','\n'.join(f"{p['title']}：{p['before_passed']} → {p['after_passed']} / {p['total']} 项通过" for p in evolution['pairs']) or '评测尚未完成')]},
            {'id':'validation-memory','title':'验证结果回写 Memory','status':'passed' if validate and all(r['state']=='done' for r in validate) else 'pending',
             'summary':'保留每次筛选和确认结果；这些分区不会作为候选生成或运行时检索的训练经验。',
             'inputs':[io('真实验证执行',f'{len(validate)} 条实际评测记录')],
             'outputs':[io('Memory 写回回执','\n'.join(receipt_line(r) for r in validate) or '等待实际验证结果')]},
            {'id':'recipe-release','title':'验证门槛与 Recipe 发布','status':'passed' if evolution['after_recipe_id'] else 'failed' if evolution['status'] in {'deferred','rejected','no_change'} else 'pending',
             'summary':evolution['status_text'],
             'inputs':[io('独立验证结论',evolution.get('reason') or '等待验证结论')],
             'outputs':[io('当前发布状态',f"Recipe v{evolution['after_revision']} 已发布" if evolution['after_recipe_id'] else '候选尚未发布；后续正式任务继续使用当前有效 Recipe'),
                        io('方法经验写回','\n'.join(receipt_line(r) for r in writes if r['partition_name']=='production') or '尚无发布方法的 Memory 回执')]}]
        # Sub-traces retain exact evaluation identity. They are not fabricated Runs.
        bundles={}
        registry=self._recipe_audit_bundles(job)
        bundles.update(registry)
        def subtraces(parts):
            result=[]
            for eid,entry in records.items():
                row=rows.get(eid)
                if not row or row['partition_name'] not in parts:continue
                rec=entry['record'];t=self._evaluation_trace(job,entry,bundles.get(rec['bundle_sha256'],{}))
                name={'train':'训练','screen':'筛选','confirm':'确认'}[row['partition_name']]
                version='原始 Recipe' if rec['bundle_sha256']==evolution['base_hash'] else '候选 Recipe'
                t['role']=f"{name} · {rec['task_id']} · {version}"
                receipt=json.loads(row['receipt_json']) if row['receipt_json'] else {}
                t['stages'].append({'id':'memory-receipt','title':'本次执行的 Memory 写回','status':'passed' if row['state']=='done' else 'pending',
                    'summary':receipt_line(row),'inputs':[io('持久化对象','本次真实执行的输出、反馈及独立评分')],
                    'outputs':[io('Memory 版本',f"{receipt.get('experience_id','待写回')} · 修订 {receipt.get('revision','未完成')}")]})
                result.append(t)
            return result
        def phase(name,role,stages,subs,status):
            return {'trace_id':name+':'+job['id'],'source':'recipe_lifecycle','role':role,'status':status,
                    'stages':stages,'subtraces':subs,'timeline':[],'trace_note':'本阶段关联真实评测、Memory 回执和版本事件。可选择单次执行查看细节'}
        learned=phase('learn','学习与校准 · Memory → Recipe',learning_stages,subtraces({'train'}),'completed' if reads and methods else 'pending')
        validated=phase('validate','验证与沉淀 · 质量 → Memory',validation_stages,subtraces({'screen','confirm'}),'completed' if evolution['available'] else 'pending')
        after=next((v for v in reversed(views) if evolution['after_recipe_id'] and v['recipe_id']==evolution['after_recipe_id']),None)
        replay=None
        if after:
            after={**after,'trace_id':after['run_id'],'role':'RSI 后 · 记忆检索与新版应用'}
            before_raw=next(r for r in runs if r['id']==source['run_id'])
            after_raw=next(r for r in runs if r['id']==after['run_id'])
            if before_raw['task']['objective']==after_raw['task']['objective'] and source.get('quality') and after.get('quality'):
                replay={'before':source['quality'],'after':after['quality'],'same_task':True}
        else:
            after=phase('after','RSI 后 · 等待新版应用',[
                {'id':'await-application','title':'等待后续任务应用新版 Recipe','status':'waiting',
                 'summary':'发布通过后，后续任务重新检索 Memory 并冻结新版 Recipe；已有快照保持不变。',
                 'inputs':[io('前置状态',evolution['status_text'])],
                 'outputs':[io('实际执行状态','尚未产生使用已发布新版的正式任务，不补造执行结果')]}],[],'waiting')
        return {'job_id':job['id'],'phases':[source,learned,validated,after],
                'complete':bool(after.get('run_id') and after['status']=='completed' and any(r['partition_name']=='production' and r['state']=='done' for r in writes)),
                'replay':replay,'memory_writes':len(writes),'memory_written':sum(r['state']=='done' for r in writes),'learning_reads':len(reads)}
