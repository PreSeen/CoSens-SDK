import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def sha(name):return hashlib.sha256((ROOT/name).read_bytes()).hexdigest()

actions=[{'id':'supplier_csv_scan','version':'1','description':'逐家供应商读取原始 CSV 并完成付款与发票核对；无数据库准备成本，保持相同业务规则。','confidence':0.5,'cost':0.0},
         {'id':'supplier_sqlite_batch','version':'1','description':'本次从零导入相同 CSV、建立 SQLite 索引，再批量完成所有供应商核对；索引构建计入成本，保持相同业务规则。','confidence':0.5,'cost':0.0}]
protocol={'schema':'supplier-feedback-replay-v1','task_family':'supplier-reconciliation-luna-20261023-v1','actions':actions,
 'environment':{'case_id':'supplier-reconciliation-luna-20261023-v1','case_title':'中文供应商月结对账：一次反馈后的 RSI 选路改善',
                'executor_model':'gpt-6-luna','fixture_version':'fictional-supplier-2026-09-v1','experiment_design':'online-feedback-replay',
                'requirements_sha256':sha('requirements.md'),'engine_sha256':sha('engine.py')},
 'observation':'为 18 家供应商完成同一 2026 年 9 月账套的付款发票核对；原始账套 80000 张发票、81631 条付款记录；目标为批次计算 2 秒以内，要求精确异常清单，数据库冷建成本必须计入。',
 'reward':{'spec':'verified-decision-return-v1','sla_seconds':2.0,'formula':'exact match ? max(-1,min(1,(2-median_seconds)/2)) : -1'},
 'design':'Two separate GPT-6 Luna sessions. First real result is independently verified and written to Memory before replay. Same dataset, methods, priors and evaluation; no holdout generalization claim. Tie breaks by first candidate.',
 'timing_scope':'Complete reconciliation calculation including fresh index import/build and exception CSV; excludes model/MCP, hash checks and independent oracle.'}
(ROOT/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2)+'\n')
files=['requirements.md','protocol.json','engine.py','oracle.py','measure.py','data/invoices.csv','data/payments.csv','data/requests.json']
(ROOT/'input-lock.json').write_text(json.dumps({name:sha(name) for name in files},ensure_ascii=False,indent=2)+'\n')
for label,role in [('baseline','首次执行，无本任务已验证经验'),('after','首次反馈写回后的重新执行')]:
    prompt=f'''执行模型固定 gpt-6-luna，不要换模型或委派其他代理。
请应用本机 cosens-rsi:rsi-run 技能，通过 cosens_rsi MCP 完成中文供应商月结对账。本次角色：{role}。根目录 /home/zhoujunjie/CoSens，案例目录 business_cases/supplier_reconciliation，唯一输出目录 work/{label}。
先读插件技能 /home/zhoujunjie/.codex/plugins/cache/cosens-local/cosens-rsi/0.1.0/skills/rsi-run/SKILL.md，以及案例的 requirements.md、protocol.json、data/requests.json。不得修改输入、脚本、配置或服务。不要读取另一个 work、var、凭证、operator.py 或过去的评估结果。不要调用独立 Memory MCP、管理员、反馈验收或发布接口；Memory 由 RSI 内部访问。普通 MCP 请求是本次任务授权的一部分。
用 cosens_prepare_task 只准备一次，objective 使用“为财务负责人完成2026年9月18家供应商付款与发票对账，定位缺票、供应商错配及多付少付，提交异常清单和中文处理建议：{role}”。task_family 与 environment 原样使用 protocol.json，execution_mode=guided，requires_evidence=true。该 family 为未验证的通用 seed Recipe，不声称 Recipe 已进化。
按 next_step 返回的 prepare -> produce -> verify 合同走完全流程，prepare 的 brief 为非空中文文本。produce 发出后请求 cosens_request_guidance，node_id=produce、candidates=protocol.actions 原样、observation=protocol.observation 原样、request_key=supplier-reconcile-v1。实际按 RSI 推荐的数据访问路线执行；不能改分数、候选或为制造改善另选路线。未尝试的路线不等于已证明更好。
调用真实测量命令：.venv/bin/python business_cases/supplier_reconciliation/measure.py --route <推荐ID> --output-dir business_cases/supplier_reconciliation/work/{label}
工具会执行三轮新进程、逐行核对独立参考，并保存 measurement.json 和 异常清单.csv；已有测量不得覆盖，失败必须保留。读取真实指标，编写本输出目录 供应商对账报告.md：含业务目标、仿真数据声明、精确金额与单位、异常分类统计、至少 5 个真实异常样例、分级处理建议、执行路线和实测耗时、统计范围与局限。分析业务结果，不要全量贴 JSON。不能把应付待核或超付待核金额说成确定损失/追回收益。不能假称用户验收、外部奖励通过或模型本身提速。
produce 的产物均通过 cosens_store_artifact 上传 measurement.json、异常清单.csv、供应商对账报告.md，files 仅用真实 artifact_id/content_hash。submit_step 绑定真实 decision_id/actual_action_id。
verify 阶段真实读取已保存的三个文件，核对三次 exact_oracle_match=true、金额口径、CSV异常条数和报告一致，编写 核验记录.md 并上传；verify.files 必须含前述三个产物以及核验记录的引用。不得对同一测量重复跑另一条路线。cosens_complete_run 后回查 get_run 和 get_decision，确认 completed 和实际动作写回 done（此时 reward 为 pending；操作员另行独立评估）。
最后 work/{label}/run.json 写真实元数据：{{"label":"{label}","run_id":"...","decision_id":"...","action_id":"...","measurement_artifact":{{"artifact_id":"...","content_hash":"..."}},"report_artifact":{{"artifact_id":"...","content_hash":"..."}}}}。不含 attempt_token 或凭证。持续完成本次 Run；如阻塞如实写出，不得伪造结果。不要开启下一次执行。
'''
    (ROOT/f'{label}.prompt.md').write_text(prompt)
print('protocol, input lock and two executor prompts frozen')
