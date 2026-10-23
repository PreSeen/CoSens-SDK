请使用本机已安装的 cosens-rsi:rsi-run skill 与 cosens_rsi MCP，执行真实电商经营分析任务的基准和校准阶段。所有业务数据均来自下载好的 UCI 真实历史交易，绝不生成模拟订单。本阶段交付基准月报和可追踪的校准任务，不是服务 smoke。

工作目录 /home/zhoujunjie/CoSens；case 目录 business_cases/retail_operations。
先阅读该目录的 protocol.json、input-lock.json、data/raw/provenance.json、data/index_manifest.json 和本地插件 rsi-run skill。protocol 的 candidates、environment、observation、训练/留出划分和奖励公式都已在运行前固定。不要修改协议、数据、工具脚本、依赖、RSI 服务或任何现有项目代码。只允许在 case/output 下生成交付文件和 stage1_runs.json；正常 MCP 会持久化相应任务数据。

实际业务目标：为电商运营负责人生成月度销售/负向记账/国家贡献/已识别客户当月复购分析和行动建议。使用 GBP，有完整口径，不能把净记账额称为利润，不能把负向记账全部说成实物退货，不能把月内重复下单率说成留存率，不能把 2011 年数据说成当前经营。不得暴露或读取任何服务凭证。

按顺序完成三个独立 guided Run，task_family 必须为 protocol.task_family（retail-repeat-report-v1），requires_evidence=true，environment 必须原样使用 protocol.environment；每个 Run 只 prepare 一次。此 family 已由本地操作员启用通用、未做业务效果验证的 prepare/produce/verify seed Recipe，必须如实报告其状态。各 Run 的中文 objective 指定月份和本次任务角色，不要往 environment 额外加入月份/基准/校准字段。

Run A：2011-11 经营月报基准，尚无本业务的已验证经验。prepare 节点输出业务简报 brief:text；produce 节点先调用 request_guidance，绑定 node_id，candidates 原样使用 protocol.actions，observation 原样使用 protocol.observation，request_key 自定稳定文本。按照 recommendation 实际调用下面的测量工具。两候选同分时原始 Excel 在前是预先披露的确定性平局规则，不声称这代表不受约束的 Codex 最优决策。生成 output/baseline_monthly_summary.md（中文业务分析，含数据局限）和指标证据。

Run B：2011-01 经营月报，预先约定的平衡探索校准原始 Excel 路线，实际动作必须为 raw_xlsx。
Run C：2011-02 经营月报，预先约定的平衡探索校准索引路线，实际动作必须为 indexed_sqlite。
Run B/C 仍必须先在 produce 请求同样 candidates/observation 的 RSI guidance，再执行协议约定的探索动作；如推荐与探索动作不同必须明确记录，不伪装成执行了推荐。每个 Run 的 prepare/produce/verify 都走服务发出的 next_step/submit_step 合同；produce 的 submit_step 记录真实 decision_id/actual_action_id。verify 读取实际生成的文件，检查所有三次 trial exact_oracle_match、单位与统计口径，然后提交 artifacts 引用，最后 complete_run。不能使用 submit_result 绕过 guided 协议。

真正执行测量的命令（将 engine、period、out 按任务替换）：
business_cases/retail_operations/.venv/bin/python business_cases/retail_operations/measure.py --engine raw_xlsx --period 2011-11 --repeats 3 --out output/baseline_measurement.json
Run B 使用 --engine raw_xlsx --period 2011-01 --out output/training_raw_measurement.json。
Run C 使用 --engine indexed_sqlite --period 2011-02 --out output/training_indexed_measurement.json。
该工具在独立新进程中真实计时、逐项与独立 OOXML oracle 精确比对并拒绝覆写。请读取其实际结果，不要猜测耗时、奖励或 KPI；校准不需要你自己运行评分 API。工具输入路径都在 case 内，所以不需要联网或访问健康接口。

所有产物先用 cosens_store_artifact 存为不可变文本 artifact，然后按输出端口提交（brief 端口为非空文本，files 端口为 artifact_id/content_hash 引用数组）。produce/verify 可分别存指标 manifest 和中文业务摘要。读取 Run/events/decision，等待每条 upsert outbox 为 done；此时奖励仍应为 pending。禁止调用任何独立 Memory MCP、管理员、evaluation、improvement、审批、发布或自动接受反馈接口。Memory 检索只能由 RSI 内部完成，对不相关的旧测试证据要说明未采用。不要提前运行后验 Run 或验证奖励；外部可信校验器会在本阶段结束后独立提交有证据的训练评估。

最后写 output/stage1_runs.json，schema 为：
{"baseline":{"run_id":"...","decision_id":"...","action_id":"...","measurement":"output/baseline_measurement.json"},"training_raw":{"run_id":"...","decision_id":"...","action_id":"raw_xlsx","measurement":"output/training_raw_measurement.json"},"training_indexed":{"run_id":"...","decision_id":"...","action_id":"indexed_sqlite","measurement":"output/training_indexed_measurement.json"}}
必须使用真实返回 ID，不得包含 attempt_token 或任何凭证。最终答复用中文给实际任务结果与阶段状态。持续执行直到三个 Run completed，或给出真实阻塞证据。
