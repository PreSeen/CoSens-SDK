"""Operator-configured GPT-6 Luna adapter for the isolated Chinese Recipe demo.

Only public case material and the frozen Recipe reach the actor. The proposer
receives train records only. Tests, screen cases and confirm cases stay out.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from uuid import uuid4

from .algorithms.hierarchical import ExecutionBudget, fingerprint, mutate
from .algorithms.hierarchical_learning import LearningPolicy
from .evaluation import LocalHarnessBackend

ROOT = Path(__file__).resolve().parents[2] / 'business_cases/refund_recipe_quality'
FAMILY = 'refund-recipe-quality-luna-v1'


def reference(d):
    order = d['order']; lines = order['lines']
    chosen = max((p for p in d['policies'] if p['effective_from'] <= order['paid_at']), key=lambda p:p['effective_from'])
    eligible = {l['line_id']:l['quantity']*l['unit_price_cents'] for l in lines if l['coupon_eligible'] and l['unit_price_cents'] > 0}
    denominator = sum(eligible.values()); coupon = order['coupon_cents']
    alloc = {k:coupon*v//denominator for k,v in eligible.items()}
    remainder = coupon-sum(alloc.values())
    priority = sorted(eligible, key=lambda k:(-(coupon*eligible[k]%denominator),k))
    for k in priority[:remainder]: alloc[k] += 1
    paid, old_qty, seen = {}, {}, set()
    for event in d['refund_ledger']['events']:
        line = event['line_id']
        old_qty[line] = max(old_qty.get(line,0),event['approved_total_qty'])
        if event['status'] != '已到账' or event['refund_id'] in seen: continue
        seen.add(event['refund_id']);paid[line] = paid.get(line,0)+event['amount_cents']
    expected=[]
    for line in lines:
        key=line['line_id'];qty=line['quantity'];net=qty*line['unit_price_cents']-alloc.get(key,0)
        official=[a for a in d['approval_records'] if a['line_id']==key and a['signed_by'] and a['status'] in {'已批准','拒绝'}]
        latest=max(official,key=lambda a:a['signed_at']) if official else None
        confirmed=max(old_qty.get(key,0),latest['approved_total_qty'] if latest and latest['status']=='已批准' else 0)
        state='拒绝' if latest and latest['status']=='拒绝' else '补证' if d['request']['requested_total_qty'][key]>confirmed else '批准'
        refs=[order['source_id'],chosen['source_id'],latest['source_id'] if latest else d['request']['source_id']]
        if old_qty.get(key,0) or paid.get(key,0):refs.append(d['refund_ledger']['source_id'])
        expected.append({'line_id':key,'coupon_allocated_cents':alloc.get(key,0),'net_cents':net,'approved_total_qty':confirmed,
                         'prior_refunded_cents':paid.get(key,0),'refund_due_cents':max(0,net*confirmed//qty-paid.get(key,0)),
                         'status':state,'evidence_refs':refs})
    full=all(e['approved_total_qty']>=l['quantity'] for l,e in zip(lines,expected) if l['unit_price_cents']>0)
    any_return=any(e['approved_total_qty']>0 for e in expected)
    shipping=max(0,order['shipping_paid_cents']-paid.get('shipping',0)) if order['merchant_fault'] and (full or chosen['old_allow_partial_shipping'] and any_return) else 0
    return {'policy_id':chosen['policy_id'],'policy_conflicts':[p['source_id'] for p in d['policies'] if p!=chosen],
            'lines':expected,'shipping_refund_cents':shipping,'cash_total_cents':sum(e['refund_due_cents'] for e in expected)+shipping,
            'reconciled_goods_paid_cents':sum(e['net_cents'] for e in expected)}


def grade(workspace, spec):
    checks=[];d=spec['documents'];expected=reference(d)
    if not (Path(workspace)/'result.json').is_file() or not (Path(workspace)/'审核报告.md').is_file():
        return {'judge_valid':False,'score':None,'task_success':False,'quality_checks':[],
                'feedback':'工件未被执行器正确定位，业务评分无效。'}
    def check(key,label,passed,critical=False):
        checks.append({'key':key,'title':label,'passed':bool(passed),'critical':critical})
    try:actual=json.loads((Path(workspace)/'result.json').read_text())
    except (OSError,ValueError):actual={}
    lines=actual.get('lines',[]) if isinstance(actual,dict) else [];actual=actual if isinstance(actual,dict) else {}
    by_id={r.get('line_id'):r for r in lines if isinstance(r,dict)} if isinstance(lines,list) else {}
    check('coverage','所有商品逐行覆盖且无重复',len(lines)==len(expected['lines']) and set(by_id)=={l['line_id'] for l in expected['lines']})
    check('policy','按订单生效日选正确政策',actual.get('policy_id')==expected['policy_id'],True)
    check('conflicts','识别并排除不适用政策',isinstance(actual.get('policy_conflicts'),list) and set(actual['policy_conflicts'])==set(expected['policy_conflicts']))
    known={d[k]['source_id'] for k in ['order','request','refund_ledger','invoice','service_note']}|{p['source_id'] for p in d['policies']}|{a['source_id'] for a in d['approval_records']}
    fields={'coupon_allocated_cents':'优惠券精确分摊','net_cents':'实际支付基数','approved_total_qty':'只采用正式累计审批',
            'prior_refunded_cents':'已到账退款去重','refund_due_cents':'累计退款取整后减历史款','status':'明确已批与待批边界'}
    for row in expected['lines']:
        received=by_id.get(row['line_id'],{})
        for field,title in fields.items():
            value=received.get(field);wanted=row[field]
            check(row['line_id']+':'+field,row['line_id']+' · '+title,value==wanted and (type(value) is int if type(wanted) is int else isinstance(value,str)),field in {'refund_due_cents','approved_total_qty'})
        refs=received.get('evidence_refs')
        check(row['line_id']+':evidence',row['line_id']+' · 证据完整且来源有效',isinstance(refs,list) and all(isinstance(r,str) for r in refs) and set(row['evidence_refs'])<=set(refs)<=known and not set(refs)&set(expected['policy_conflicts']))
    for key,title in [('shipping_refund_cents','运费条件与历史退款正确'),('cash_total_cents','本次现金退款总额精确'),('reconciled_goods_paid_cents','按订单而非发票展示额核对实付')]:
        check(key,title,type(actual.get(key)) is int and actual[key]==expected[key],key!='reconciled_goods_paid_cents')
    reply=actual.get('customer_reply','');reply=reply if isinstance(reply,str) else ''
    check('reply_amount','中文回复准确列本次金额',f"{expected['cash_total_cents']/100:.2f}元" in reply and re.search('[\u4e00-\u9fff]',reply))
    check('privacy','回复不泄露完整手机或银行卡',d['request']['customer_phone'] not in reply and d['request']['customer_bank'] not in reply,True)
    check('no_promise','不编造确定到账承诺',bool(reply) and not any(p in reply for p in ['保证明天到账','一定明天到账','保证24小时到账']) and '支付渠道' in reply and '回执' in reply)
    pending=[e['line_id'] for e in expected['lines'] if e['status']=='补证']
    uncertainties=actual.get('uncertainties',[])
    uncertainties=uncertainties if isinstance(uncertainties,list) else []
    substantive=all(any(isinstance(text,str) and key in text and re.search('审批|待批|补证',text)
                        for text in uncertainties) for key in pending)
    check('uncertainty','每个待批商品有实质说明',not pending or substantive and bool(re.search(r'待.{0,8}(?:审批|补)|尚未批准|待批',reply)))
    try:report=(Path(workspace)/'审核报告.md').read_text()
    except OSError:report=''
    check('report','中文报告包含政策依据与本次金额',expected['policy_id'] in report and (f"{expected['cash_total_cents']/100:.2f}" in report or str(expected['cash_total_cents']) in report) and bool(re.search('[\u4e00-\u9fff]',report)))
    passed=sum(c['passed'] for c in checks)
    failures=[c['title'] for c in checks if not c['passed']]
    return {'judge_valid':True,'score':passed/len(checks),'task_success':passed==len(checks),
            'quality_checks':checks,'passed':passed,'total':len(checks),
            'feedback':'独立业务检查：'+('全部通过' if not failures else '未通过：'+'；'.join(failures))}


def model_command(workspace, prompt, timeout):
    """Same fixed model, reasoning budget and tools for every Recipe arm."""
    from .runtime import run_codex
    return run_codex(workspace, prompt, timeout)


class RefundDriver:
    def __init__(self, workspace): self.workspace=Path(workspace)

    def execute(self,instruction,*,timeout):
        start=time.monotonic()
        prompt=('执行冻结 Recipe 的这个审核节点，模型固定 GPT-6 Luna。只使用当前工作区的公开任务材料；不得读外部目录、评估器或其他任务，不得联网或调用 MCP。可以用本地 Python 精确计算和自编检查。\n'
                '必须实际保存 result.json 和 审核报告.md。完成全部方法步骤并自查后结束；不要假称外部验收通过。\n'+instruction)
        result=model_command(self.workspace,prompt,timeout)
        normalized=[]
        for name in ['result.json','审核报告.md']:
            if (self.workspace/name).is_file():continue
            candidates=[p for p in self.workspace.rglob(name) if p.is_file() and p.resolve().is_relative_to(self.workspace.resolve())]
            if len(candidates)==1:
                shutil.copyfile(candidates[0],self.workspace/name)
                normalized.append({'source':str(candidates[0].relative_to(self.workspace)),'target':name,
                                   'sha256':hashlib.sha256((self.workspace/name).read_bytes()).hexdigest()})
        (self.workspace/'artifact-normalization.json').write_text(json.dumps(normalized,ensure_ascii=False,indent=2))
        outputs=[name for name in ['result.json','审核报告.md'] if (self.workspace/name).is_file()]
        log=['# 实际执行记录\n\n下列内容来自可观察的 Codex 工具事件，不包含模型内部思维。\n']
        for line in result.stdout.splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            item=e.get('item',{})
            if e.get('type')!='item.completed':continue
            if item.get('type')=='command_execution':
                log.append('## 运行命令\n\n```text\n'+item.get('command','')[:3000]+'\n```\n\n输出：\n\n```text\n'+item.get('aggregated_output','')[:5000]+'\n```\n')
            elif item.get('type')=='file_change':
                log.append('## 保存文件\n\n'+'\n'.join(str(c.get('path','')) for c in item.get('changes',[]))+'\n')
        (self.workspace/'执行记录.md').write_text('\n'.join(log));outputs.append('执行记录.md')
        # Preserve actual tool logs as an audit artifact, not as complete telemetry.
        response=(self.workspace/'result.json').read_text()[:14000] if (self.workspace/'result.json').is_file() else '未生成结果'
        complete=result.returncode==0 and all((self.workspace/n).is_file() for n in ['result.json','审核报告.md'])
        return {'completed':complete,'outputs':{'files':outputs},'elapsed_sec':time.monotonic()-start,
                'response':response,'error':None if complete else 'codex_execution_or_artifact_discovery_failed'}


def propose(payload):
    # No dataset lookup here. Only the learner's TRAIN payload reaches this call.
    directory=ROOT/'optimizer'/uuid4().hex;directory.mkdir(parents=True)
    safe=deepcopy(payload)
    prompt=('你是 Recipe 优化器。仅依据以下 train_evidence 中的公开任务、实际输出和失败检查，提出可复用方法改进；不得请求筛选/确认工单或参考答案。\n'
            'target 必须是 bundle.bindings 的键，不能填写 steps 的版本引用。可选 target：'+json.dumps(list(payload['bundle']['bindings']))+'。\n'
            '请实际保存 proposal.json，内容严格为 instruction 所要求的 JSON。提供两种不同的最小变更，均针对同一 binding 和同一 slot；不要更改评估器、模型、工具、预算、I/O。只写通用操作规则，不包含工单ID、商品名、源文档ID、文件名或案例金额。'
            '若训练未呈现可学习缺陷，则按协议返回 layer=none 与空 changes，不能为了展示效果虚构失败。\n'+json.dumps(safe,ensure_ascii=False))
    result=model_command(directory,prompt,600)
    if result.returncode:raise RuntimeError('Recipe proposer model failed')
    output=json.loads((directory/'proposal.json').read_text())
    # Canonicalize only an unambiguous version-reference alias. Never alter
    # learned rules or choose a candidate on behalf of the quality gates.
    normalized=[]
    for change in output.get('changes',[]):
        if change.get('layer')=='step' and change.get('target') not in payload['bundle']['bindings']:
            aliases=[k for k,v in payload['bundle']['bindings'].items() if v==change.get('target')]
            if len(aliases)==1:
                normalized.append({'original_target':change['target'],'binding':aliases[0]})
                change['target']=aliases[0]
        mutate(payload['bundle'],change)
    (directory/'validated-proposal.json').write_text(json.dumps(output,ensure_ascii=False,indent=2))
    (directory/'reference-normalization.json').write_text(json.dumps(normalized,ensure_ascii=False,indent=2))
    (directory/'train-payload.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2))
    return output


def create_backends():
    if not (ROOT/'input-lock.json').is_file():return {}
    lock=json.loads((ROOT/'input-lock.json').read_text())
    for name,digest in lock.items():
        path=ROOT/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise RuntimeError('Frozen refund demo input changed')
    cases=json.loads((ROOT/'cases.private.json').read_text());protocol=json.loads((ROOT/'protocol.json').read_text())
    code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {FAMILY:LocalHarnessBackend(cases,RefundDriver,driver_version='gpt-6-luna-refund-actor-v1:'+code_hash,
        comparator=grade,comparator_version='integer-refund-quality-v1:'+code_hash,proposer=propose,
        policy=LearningPolicy(**protocol['policy']),budget=ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),train_rollouts=2)}
