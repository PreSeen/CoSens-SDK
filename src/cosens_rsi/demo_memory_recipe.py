"""Operator-owned adapter for a fresh GPT-6 Luna Memory/Recipe closure case."""
import hashlib,json
from pathlib import Path
from uuid import uuid4
from .demo_refund import RefundDriver,grade,model_command
from .demo_sla import create_backends as existing_backends
from .evaluation import LocalHarnessBackend
from .algorithms.hierarchical import ExecutionBudget
from .algorithms.hierarchical_learning import LearningPolicy

ROOT=Path(__file__).resolve().parents[2]/'business_cases/memory_recipe_closure'
FAMILY='memory-recipe-closure-luna-v1'


def propose(payload):
    if not payload.get('memory_learning',{}).get('references'):
        raise ValueError('Demo requires actual Memory-backed train evidence')
    folder=ROOT/'optimizer'/uuid4().hex;folder.mkdir(parents=True)
    prompt=('你是RSI Recipe优化器，执行器固定GPT-6 Luna。仅使用以下从Memory读取的训练执行记录和当前Recipe，找出可复用的方法缺陷。\n'
            '按instruction实际保存 proposal.json。返回两个不同的最小方法变更，针对同一binding、同一slot；target必须来自bundle.bindings的键。'
            '不得查看其他工单、外部目录、评估器，不得联网，不得调用MCP或委派代理。不要把具体工单编号、商品名、源ID、金额或答案写进Recipe。'
            '不修改模型、预算、I/O、验收规则。没有真实方法缺陷就返回none与空changes，不能虚构改善。\n'+json.dumps(payload,ensure_ascii=False))
    result=model_command(folder,prompt,600)
    if result.returncode:raise RuntimeError('Recipe proposal failed')
    value=json.loads((folder/'proposal.json').read_text())
    (folder/'memory-train-payload.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    return value


def create_backends():
    backends=existing_backends()
    if not (ROOT/'input-lock.json').is_file():return backends
    for name,digest in json.loads((ROOT/'input-lock.json').read_text()).items():
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('Closure demo frozen material changed')
    cfg=json.loads((ROOT/'protocol.json').read_text())
    version=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    backends[FAMILY]=LocalHarnessBackend(json.loads((ROOT/'cases.private.json').read_text()),RefundDriver,
        driver_version='gpt-6-luna-refund-actor-v1',comparator=grade,comparator_version='integer-refund-quality-v2',
        proposer=propose,policy=LearningPolicy(**cfg['policy']),train_rollouts=cfg['train_rollouts'],
        budget=ExecutionBudget(max_calls=2,max_seconds=650,per_call_seconds=600))
    return backends
