"""Real local Codex code repair adapter for Chinese SLA Recipe quality learning."""
from copy import deepcopy
import hashlib,json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
from uuid import uuid4
from .algorithms.hierarchical import ExecutionBudget,mutate
from .algorithms.hierarchical_learning import LearningPolicy
from .evaluation import LocalHarnessBackend
from .demo_refund import model_command,create_backends as refund_backends

ROOT=Path(__file__).resolve().parents[2]/'business_cases/sla_recipe_quality'
FAMILY='sla-recipe-quality-luna-v1'


def grade(workspace,spec):
    code=Path(workspace)/'sla.py'
    if not code.is_file():return {'judge_valid':False,'score':None,'task_success':False,'quality_checks':[],'feedback':'缺少代码工件，评测无效'}
    with tempfile.TemporaryDirectory(prefix='sla-independent-') as folder:
        source=Path(folder)/'sla.py';source.write_bytes(code.read_bytes());data=Path(folder)/'scenario.json';data.write_text(json.dumps(spec))
        try:
            p=subprocess.run([sys.executable,'-I',str(ROOT/'checker.py'),str(source),str(data)],cwd=folder,
                             env={'PATH':os.defpath,'PYTHONIOENCODING':'utf-8'},capture_output=True,text=True,timeout=25)
        except subprocess.TimeoutExpired:
            return {'judge_valid':True,'score':0,'task_success':False,'quality_checks':[{'key':'timeout','title':'业务函数在预算内完成','passed':False,'critical':True}],'feedback':'代码回放超时'}
        if p.returncode:return {'judge_valid':False,'score':None,'task_success':False,'quality_checks':[],'feedback':'独立评估器未完成'}
        return json.loads(p.stdout)


class SlaDriver:
    def __init__(self,workspace):self.workspace=Path(workspace)
    def execute(self,instruction,*,timeout):
        start=time.monotonic()
        prompt=('用固定 GPT-6 Luna 执行当前 Recipe 代码修复节点。只读写本任务工作区，不联网，不调用MCP或委派代理，不读取评估器或其他工单。\n'
                '实际修改当前目录 sla.py 并保存 修复说明.md；可以新增并运行自己的测试。按方法完成修复和自查；不要声称外部验收通过。\n'+instruction)
        result=model_command(self.workspace,prompt,timeout)
        if not (self.workspace/'修复说明.md').is_file():
            candidates=[p for p in self.workspace.rglob('修复说明.md') if p.is_file() and p.resolve().is_relative_to(self.workspace.resolve())]
            if len(candidates)==1:shutil.copyfile(candidates[0],self.workspace/'修复说明.md')
        logs=['# 实际代码修复执行记录\n\n来自可观察工具事件，不包含内部思维。\n']
        for line in result.stdout.splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            item=e.get('item',{})
            if e.get('type')=='item.completed' and item.get('type')=='command_execution':
                logs.append('## 命令\n\n```text\n'+item.get('command','')[:3000]+'\n```\n\n输出：\n\n```text\n'+item.get('aggregated_output','')[:5000]+'\n```\n')
        (self.workspace/'执行记录.md').write_text('\n'.join(logs))
        files=[n for n in ['sla.py','修复说明.md','执行记录.md'] if (self.workspace/n).is_file()]
        files.extend(p.name for p in self.workspace.glob('test*.py') if p.is_file())
        complete=result.returncode==0 and (self.workspace/'修复说明.md').is_file()
        return {'completed':complete,'outputs':{'files':files},'elapsed_sec':time.monotonic()-start,
                'response':(self.workspace/'sla.py').read_text()[:18000], 'error':None if complete else 'code_execution_incomplete'}


def propose(payload):
    folder=ROOT/'optimizer'/uuid4().hex;folder.mkdir(parents=True)
    prompt=('你是 Recipe 优化器。仅使用以下训练记录中的公开任务、实际代码、失败回放和当前方法，诊断一个可复用方法缺陷，生成两种同一binding、同一slot的最小方法变更。不要访问其他工单或评估器，不要复制代码答案、函数名、文件名、日期或案例数字到Recipe。\n'
            '只将通用修复、验证、反例或区间建模方法写进Recipe；可根据真实失败学习领域程序。target必须从bindings键中选择：'+json.dumps(list(payload['bundle']['bindings']))+'。遵守 instruction；无真实可学习缺陷时返回none。\n'
            '实际保存 proposal.json，不要修改模型、工具、预算、I/O或评估器。\n'+json.dumps(payload,ensure_ascii=False))
    result=model_command(folder,prompt,600)
    if result.returncode:raise RuntimeError('SLA proposer execution failed')
    value=json.loads((folder/'proposal.json').read_text())
    for change in value.get('changes',[]):
        if change.get('layer')=='step' and change.get('target') not in payload['bundle']['bindings']:
            aliases=[k for k,v in payload['bundle']['bindings'].items() if v==change.get('target')]
            if len(aliases)==1:change['target']=aliases[0]
        mutate(payload['bundle'],change)
    (folder/'validated-proposal.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
    (folder/'train-payload.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    return value


def create_backends():
    backends=refund_backends()
    lock=json.loads((ROOT/'input-lock.json').read_text())
    for name,digest in lock.items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Frozen SLA input changed')
    module_hash=hashlib.sha256(Path(__file__).read_bytes()+(ROOT/'reference.py').read_bytes()+(ROOT/'checker.py').read_bytes()).hexdigest()
    protocol=json.loads((ROOT/'protocol.json').read_text())
    backends[FAMILY]=LocalHarnessBackend(json.loads((ROOT/'cases.private.json').read_text()),SlaDriver,
        driver_version='gpt6-luna-sla-v1:'+module_hash,comparator=grade,comparator_version='sla-interval-oracle-v1:'+module_hash,
        proposer=propose,policy=LearningPolicy(**protocol['policy']),budget=ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600),train_rollouts=2)
    return backends
