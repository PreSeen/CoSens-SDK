"""Task Prompt → 首次执行 → Recipe V1 → 用户确认原因 → V2、V3 → 同簇任务复用。

直接修改文件末尾的参数后运行。需要服务应用 005_unified_recipe_lifecycle.sql。
首次执行材料包括实际 Memory 返回；改进在用户明确选择原因后才更新 Recipe。
"""
from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from openai import AsyncOpenAI
from cosens import CoSensClient


# 两个任务属于同一个任务簇，业务输入是上次测试使用的仿真账本。
TASK_1 = """请为{company}完成 2026 年 9 月的渠道结算对账，截止日期为 2026-09-30。
所有金额均为人民币分，以下退款金额为带符号金额。

结算记录：
- S-101：青岚，结算日期 2026-09-24，销售额 100000，退款 -10000（状态 R7），手续费 2000，暂扣准备金 3000。
- S-102：赤砂，结算日期 2026-09-28，销售额 70000，退款 -5000（状态 P2），手续费 1000，暂扣准备金 2000。
- S-103：青岚，结算日期 2026-09-25，销售额 50000，退款 -1000（状态 R7），手续费 1000，暂扣准备金 0。
- S-104：青岚，结算日期 2026-09-26，销售额 20000，退款 0（状态 R7），手续费 500，暂扣准备金 0。

银行流水：
- 回执 R-01，批次 S-101，付款方沪澜结算服务，金额 40000。
- 回执 R-02，批次 S-101，付款方沪澜结算服务，金额 45000。
- 回执 R-02，批次 S-101，付款方沪澜结算服务，金额 45000。
- 回执 R-03，批次 S-103，付款方沪澜结算服务，金额 47000。
- 回执 R-04，批次 S-104，付款方沪澜结算服务，金额 19500。

请结合已有历史规则、付款主体映射和用户纠错经验，检查企业范围及规则生效日期；
逐批列出已对平、待到账、少付和需要确认的记录，给出金额、原因和来源。证据不足时明确说明，不要猜测。"""

TASK_2 = """请为{company}完成 2026 年 10 月的渠道结算对账，截止日期为 2026-10-31。
所有金额均为人民币分，以下退款金额以正数表示。

结算记录：
- X-201：青岚，结算日期 2026-10-23，销售额 210000，退款 20000（状态 R7），手续费 3000，暂扣准备金 5000。
- X-202：青岚，结算日期 2026-10-24，销售额 90000，退款 10000（状态 P2），手续费 2000，暂扣准备金 1000。
- X-203：赤砂，结算日期 2026-10-29，销售额 60000，退款 5000（状态 R7），手续费 1000，暂扣准备金 1000。
- X-204：青岚，结算日期 2026-10-26，销售额 80000，退款 5000（状态 R7），手续费 1000，暂扣准备金 0。
- X-205：青岚，结算日期 2026-10-25，销售额 40000，退款 0（状态 R7），手续费 1000，暂扣准备金 0。
- X-206：玄塔，结算日期 2026-10-25，销售额 10000，退款 0（状态 R7），手续费 200，暂扣准备金 0。

银行流水：
- 回执 N-01，批次 X-201，付款方星渡渠道资金中心，金额 91000。
- 回执 N-02，批次 X-201，付款方星渡渠道资金中心，金额 91000。
- 回执 N-02，批次 X-201，付款方星渡渠道资金中心，金额 91000。
- 回执 N-03，批次 X-204，付款方沪澜结算服务，金额 70000。
- 回执 N-04，批次 X-205，付款方陌生结算中心，金额 39000。

请结合已有历史规则、付款主体映射和用户纠错经验，检查企业范围及规则生效日期；
逐批列出已对平、待到账、少付和需要确认的记录，给出金额、原因和来源。证据不足时明确说明，不要猜测。"""


async def execute_request(rsi, native, execution, *, prompt, memory, model):
    """执行服务冻结的请求，并提交本次真实输出。"""
    request = execution['request']
    method = request.get('method')
    context = {'memory': memory, 'method': method,
               'feedback': request.get('feedback', []), 'confirmed_cause': request.get('cause')}
    response = await native.chat.completions.create(model=model, messages=[
        {'role': 'system', 'content': '完成用户任务。下列内容是本次方法与历史证据；核对来源和适用范围，证据不足时说明缺口。\n' + json.dumps(context, ensure_ascii=False)},
        {'role': 'user', 'content': prompt},
    ])
    answer = response.choices[0].message.content
    if not answer:
        raise RuntimeError('模型没有返回文本；保留 execution_id 后核查，避免盲目重跑')
    output = await rsi.atoms.artifact(name='answer.txt', content=answer)
    events = [{'id': 'model-response', 'kind': 'model_result',
               'payload': {'model': model, 'output_artifact_id': output['artifact_id'],
                           'supplied_recipe_candidate_id': method.get('recipe_candidate_id') if method else None}}]
    receipt = await rsi.atoms.submit_execution(execution['execution_id'],
        request_sha256=execution['request_sha256'], outcome='completed',
        artifacts=[{'artifact_id': output['artifact_id'], 'role': 'submitted_output'}],
        events=events, trace_coverage='partial')
    print(answer)
    print('执行回执：', json.dumps(receipt, ensure_ascii=False))
    return receipt


async def execute_task(rsi, native, prompt: str, *, model, model_api, recipe_id=None):
    key = uuid4().hex
    # 服务检索、校验并冻结 Memory，Agent 和 Recipe 使用同一份证据。
    task = await rsi.atoms.create_task(source='new_task', objective=prompt, key=key + ':task')
    execution = await rsi.atoms.start_execution(task['task_id'], key=key + ':execute',
                                               recipe_id=recipe_id, model_api=model_api, include_memory=True)
    memory = execution['request']['memory']
    receipt = await execute_request(rsi, native, execution, prompt=prompt, memory=memory, model=model)
    return task['task_id'], receipt, memory


async def wait_recipe(rsi, candidate_id):
    candidate = await rsi.atoms.wait_workflow_job('recipe_candidate', candidate_id, timeout=300)
    print('Recipe 状态：', json.dumps({k: candidate.get(k) for k in
        ('recipe_id', 'recipe_candidate_id', 'version', 'parent_candidate_id', 'status')}, ensure_ascii=False))
    if candidate['status'] != 'candidate':
        raise RuntimeError('Recipe 尚不可用；请按上面的候选 ID 恢复作业后再继续')
    return candidate


async def main(*, model_api_key: str, cosens_api_key: str, model: str, task1: str, task2: str,
               model_base_url: str | None = None, region: str = 'us'):
    if any(not isinstance(x, str) or not x.strip() or x.startswith('<')
           for x in (model_api_key, cosens_api_key, model, task1, task2)):
        raise ValueError('请传入真实 Key、模型名及两段任务 Prompt')
    if region not in {'cn', 'us'}:
        raise ValueError('region 必须是 cn 或 us')
    if model_base_url and model_base_url.startswith('<'):
        raise ValueError('请传入模型服务地址，官方 OpenAI 地址可设为 None')
    rsi = CoSensClient(api_key=cosens_api_key, region=region)
    native = AsyncOpenAI(api_key=model_api_key, base_url=model_base_url)
    model_api = {'api_key': model_api_key, 'base_url': model_base_url or 'https://api.openai.com/v1', 'model': model}
    try:
        task_id, receipt, memory = await execute_task(rsi, native, task1, model=model, model_api=model_api)
        candidate = await wait_recipe(rsi, receipt['initial_recipe_candidate_id'])
        while True:
            feedback = input('请输入对任务结果的反馈；回车结束迭代：').strip()
            if not feedback:
                break
            job = await rsi.atoms.improve_task(task_id, revision_id=receipt['revision_id'],
                                             feedback=feedback, key='feedback-' + uuid4().hex)
            if not job.get('job_id'):
                print('改进尚未启动：', job.get('status'))
                break
            reason = input('请输入你确认的原因；回车让 RSI 先分析：').strip()
            if reason:
                selected = await rsi.atoms.select_cause(job['job_id'], custom_reason=reason)
            else:
                attribution = await rsi.atoms.wait_workflow_job('attribution', job['job_id'], timeout=300)
                if attribution['status'] not in {'awaiting_selection', 'inconclusive'}:
                    print('原因分析未完成：', attribution['attribution_id'], attribution['status'])
                    break
                portfolio = attribution['portfolio']
                print(json.dumps(portfolio['diagnostic_candidates'], ensure_ascii=False, indent=2))
                selected_id = input('请输入你确认的原因候选 ID；回车暂不确认：').strip()
                if not selected_id:
                    break
                selected = await rsi.atoms.select_cause(job['job_id'], candidate_id=selected_id,
                                                       candidate_sha256=portfolio['candidate_sha256'])
            candidate = await wait_recipe(rsi, selected['recipe_candidate_id'])
            # 新版方法就绪后，Agent 使用它修订任务结果，下一次反馈绑定新结果。
            execution = await rsi.atoms.start_revision(receipt['revision_id'], attribution_id=job['job_id'],
                                                       key='revision-' + uuid4().hex)
            receipt = await execute_request(rsi, native, execution, prompt=task1, memory=memory, model=model)
        print('任务二使用 Recipe：', candidate['recipe_id'], 'V' + str(candidate['version']))
        await execute_task(rsi, native, task2, model=model, model_api=model_api, recipe_id=candidate['recipe_id'])
        versions = await rsi.atoms.get_recipe_versions(candidate['recipe_id'])
        print('版本链：', [(v['version'], v['recipe_candidate_id']) for v in versions['versions']])
    finally:
        await native.close()
        await rsi.aclose()


if __name__ == '__main__':
    company = '星桥仿真企业-memory-pair-144c56ea9260'
    asyncio.run(main(
        model_api_key='<模型服务 API Key>',
        cosens_api_key='<CoSens API Key>',
        model='deepseek-v4-flash',
        model_base_url='<模型服务 OpenAI 兼容地址>',
        region='us',
        task1=TASK_1.format(company=company),
        task2=TASK_2.format(company=company),
    ))
