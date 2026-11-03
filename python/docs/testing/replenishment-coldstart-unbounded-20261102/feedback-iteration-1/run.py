"""Synthetic user feedback for the recorded cold-start answer; no real acceptance."""
import json,time,importlib.metadata
from pathlib import Path
from cosens import CoSensClient
from openai import OpenAI

FEEDBACK='''【模拟业务用户反馈，仅用于合成测试，不代表真实用户验收】
你识别了断货风险，这个方向是对的。但不能按312件提单：你把“这笔在途第4天到货”当成了新采购的提前期，又自行假设到货后覆盖7天。虽然你说明了假设，但这些值不是我们店铺的采购口径，不能替代核验。
补充本次已确认的业务资料：A-17最近7天每天可售小时数是24、24、24、0、0、0、0；后4天确实断货。采购提前期5天、复核周期2天、安全天数2天，保护期为这三项之和。最小起订50件，包装倍数10件。当前可售库存8件已经包含今天临时调入的数量，不要重复增加。在途120件已确认，第4天到货，无其他已确认在途或调拨。
本店原始订货量按“日需求×保护期－当前可售库存－保护期内已确认在途”计算，再向上满足最小起订量和包装倍数。日需求须按实际可售时长核验，不能只用有销量的天数替代可售时长。
请据此修正订货建议和今天的优先动作。以后先核验真实采购参数；资料缺失时先指出缺口或追问，不要用自定的常见值作为实际提单依据。临时调拨尚未确认时不抵扣，确认后重新计算。'''
CAUSE='''【模拟用户确认的原因】原答案缺少企业采购先验及其核验步骤，把已有在途的到货时间用于新增采购提前期，并以自行假设的覆盖天数给出实际提单建议。应明确区分在途ETA与采购提前期，核验可售时长、真实保护期参数及起订/包装约束；缺少关键参数时追问，不把条件性假设当作已确认业务口径。'''


def run(*,cosens_api_key,model_api_key,region,model,model_base_url,base_dir):
    root=Path(base_dir);base=json.loads((root/'state.json').read_text());folder=root/'feedback-iteration-1';folder.mkdir(exist_ok=True);folder.chmod(0o700)
    state_path=folder/'state.json';tag=base['tag']
    state=json.loads(state_path.read_text()) if state_path.exists() else {'started_at':time.time(),'feedback_source':'synthetic user feedback and simulated cause confirmation; not real user acceptance',
        'feedback':FEEDBACK,'confirmed_cause':CAUSE,'calls':{},'sdk_version':importlib.metadata.version('cosens-sdk'),'model':model,
        'task_id':base['calls']['task']['response']['task_id'],'source_revision_id':base['calls']['submit']['response']['revision_id'],
        'protocol':{'output_token_limit_parameter':'omitted','client_timeout':None,'feedback_excludes_expected_order_quantity':True,'task2_executed':False}}
    secrets=[cosens_api_key,model_api_key]
    def clean(x):
        for s in secrets:
            if s:x=x.replace(s,'[REDACTED]')
        return x
    def write(name,value):
        p=folder/name;p.write_text(clean(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2))+'\n');p.chmod(0o600)
    def save():write('state.json',state)
    def call(name,fn):
        old=state['calls'].get(name)
        if old and old['status']=='completed':return old['response']
        if old:raise RuntimeError('Reconcile the recorded outcome before retrying '+name)
        state['calls'][name]={'status':'running','started_at':time.time()};save()
        try:r=fn()
        except Exception as e:
            state['calls'][name].update(status='error',error_type=type(e).__name__,http_status=getattr(e,'status_code',None),message=clean(str(e))[:500]);save();raise
        state['calls'][name].update(status='completed',response=r,finished_at=time.time());save()
        print(json.dumps({'step':name,'status':r.get('status','completed') if isinstance(r,dict) else 'completed'},ensure_ascii=False),flush=True)
        return r
    sdk=CoSensClient(api_key=cosens_api_key,region=region,state_path=str(folder/'sdk-state.sqlite3'))
    native=OpenAI(api_key=model_api_key,base_url=model_base_url,max_retries=0,timeout=None)
    try:
        tid=state['task_id'];rid=state['source_revision_id']
        before=call('before',lambda:sdk.sync_atoms.get_task_recipe(tid))
        assert before['current']['version']==1
        write('recipe-v1.json',before['current']);write('feedback.txt',FEEDBACK);write('confirmed-cause.txt',CAUSE)
        job=call('feedback',lambda:sdk.sync_atoms.improve_task(tid,revision_id=rid,feedback=FEEDBACK,key=tag+':feedback-1'))
        if not job.get('job_id'):state['status']='feedback_not_started';save();return
        selected=call('cause_selection',lambda:sdk.sync_atoms.select_cause(job['job_id'],custom_reason=CAUSE))
        cid=selected['recipe_candidate_id'];last=None
        while True:
            candidate=sdk.sync_atoms.get_recipe_candidate(cid)
            state['candidate']=candidate;save()
            if candidate['status']!=last:
                print(json.dumps({'stage':'recipe','version':candidate.get('version'),'status':candidate['status'],'candidate_id':cid}),flush=True);last=candidate['status']
            if candidate['status']=='candidate':break
            if candidate['status'] not in {'queued','running','awaiting_parent'}:
                state['status']='recipe_not_ready';save();return
            time.sleep(5)
        assert candidate['version']==2 and candidate['recipe_id']==before['recipe_id']
        assert candidate['parent_candidate_id']==before['current']['recipe_candidate_id']
        write('recipe-v2.json',candidate)
        execution=call('revision_execution',lambda:sdk.sync_atoms.start_revision(rid,attribution_id=job['job_id'],key=tag+':revision-1'))
        if execution['status']!='awaiting_execution':state['status']='revision_not_ready';save();return
        assert execution['request']['method']['recipe_candidate_id']==cid
        if state.get('model_status'):raise RuntimeError('Model attempt already exists; do not replay')
        method=execution['request']['method']
        request={'model':model,'temperature':0,'stream':True,'stream_options':{'include_usage':True},'messages':[
            {'role':'system','content':'你是一名经营分析助手。依据用户需求和可核验的信息完成任务。不要编造企业事实；资料不足时明确说明。下列是服务端提供的本次修订方法，标记为尚未独立验证的候选：\n'+json.dumps(method,ensure_ascii=False)},
            {'role':'user','content':base['task_prompt']},
            {'role':'assistant','content':base['answer']},
            {'role':'user','content':'\n'.join(item['message'] for item in execution['request']['feedback'])}]}
        write('model-request.json',request);state['model_status']='running';save();print('Revising Task1 with actual V2, without output token limit.',flush=True)
        stream=native.chat.completions.create(**request)
        parts=[];reasoning_chars=0;count=0;last_save=0;finish=None;usage=None;response_id=None
        with (folder/'model-stream.jsonl').open('w') as log:
            try:
                for chunk in stream:
                    log.write(clean(chunk.model_dump_json())+'\n');log.flush();count+=1;response_id=chunk.id or response_id
                    if chunk.usage:usage=chunk.usage.model_dump(mode='json')
                    for choice in chunk.choices:
                        if choice.delta.content:parts.append(choice.delta.content)
                        reasoning_chars+=len(getattr(choice.delta,'reasoning_content','') or '')
                        if choice.finish_reason:finish=choice.finish_reason
                    if time.time()-last_save>=5 or finish:
                        write('progress.json',{'chunks':count,'answer_chars':sum(map(len,parts)),'reasoning_chars':reasoning_chars,'finish_reason':finish,'last_update':time.time()});last_save=time.time()
            finally:stream.close()
        answer=''.join(parts);state.update(model_status='completed' if finish=='stop' and answer else 'incomplete',finish_reason=finish,usage=usage,model_response_id=response_id);save()
        write('model-response.json',{'answer':answer,'response_id':response_id,'finish_reason':finish,'usage':usage})
        if finish!='stop' or not answer:state['status']='no_final_answer';save();return
        write('answer.txt',answer);state['answer']=answer;save()
        artifact=call('output',lambda:sdk.sync_atoms.artifact(name=tag+'-revision-1.txt',content=answer))
        receipt=call('submit',lambda:sdk.sync_atoms.submit_execution(execution['execution_id'],request_sha256=execution['request_sha256'],outcome='completed',
            artifacts=[{'artifact_id':artifact['artifact_id'],'role':'submitted_output'}],events=[{'id':'model-revision','kind':'model_result','payload':{
                'model':model,'output_artifact_id':artifact['artifact_id'],'supplied_recipe_candidate_id':cid,'feedback_source':'synthetic_test_fixture'}}],trace_coverage='partial'))
        versions=call('versions',lambda:sdk.sync_atoms.get_recipe_versions(candidate['recipe_id']))
        state['status']='feedback_iteration_completed';state['finished_at']=time.time();save()
        print('REVISED_ANSWER\n'+answer,flush=True)
        print(json.dumps({'status':state['status'],'recipe_id':candidate['recipe_id'],'versions':[v['version'] for v in versions['versions']],
            'revised_revision_id':receipt.get('revision_id'),'output_dir':str(folder)},ensure_ascii=False),flush=True)
    except Exception as e:
        state.update(status='error',error_type=type(e).__name__,http_status=getattr(e,'status_code',None));save();print(json.dumps({'error_type':type(e).__name__,'http_status':getattr(e,'status_code',None)}),flush=True)
        raise RuntimeError('Recorded iteration failure: '+type(e).__name__) from None
    finally:native.close();sdk.close()

