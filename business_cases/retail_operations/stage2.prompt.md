继续本机真实电商经营月报业务，以新的 guided RSI Run 完成经验使用后的十一月任务和最终业务交付。

先读已安装 rsi-run skill、case 目录 business_cases/retail_operations 下 README.md、protocol.json、output/stage1_runs.json、output/evaluations.json，以及已经存在的基准/校准月报和测量结果。不要改动协议、输入、工具、依赖或项目应用代码。只在 case/output 下创建本阶段交付。

本地外部可信评估器已根据独立 OOXML 金额/计数核验与三轮真实进程耗时，对一月 raw_xlsx 和二月 indexed_sqlite 的实际动作提交评估，完整证据及 Memory 修订记录在 evaluations.json。你不能调用管理员或自行生成 verified reward。留出十一月的基准测量未被作为训练奖励。

必须创建一个新的 Run，cosens_prepare_task 只调用一次；task_family=protocol.task_family，execution_mode=guided，requires_evidence=true，environment 原样使用 protocol.environment。objective 为中文十一月经营月报业务目标，说明需要向运营负责人交付中文分析、指标表和看板，比较使用既有经验后的计算路线；不得给 environment 追加本阶段标记。

逐步执行服务发出的 prepare/produce/verify 三个节点。prepare 输出业务简报 brief:text；produce 先使用 protocol.observation、protocol.actions 原样请求绑定当前 node_id 的 guidance。不得修改候选、版本、描述、顺序、confidence 或 cost 以制造排序变化。记录 cold_start、推荐、分数、experience_refs、水位和实际执行路线。如推荐没有变化也按事实报告，禁止强行宣称 RSI 提升。实际 action_id 必须与真正执行的命令一致。

调用同一个受信工具进行三轮真实留出测量（engine 按 RSI recommendation）：
business_cases/retail_operations/.venv/bin/python business_cases/retail_operations/measure.py --engine indexed_sqlite --period 2011-11 --repeats 3 --out output/post_measurement.json
此命令的 engine 参数只是示例，必须跟随本次真实推荐。不得覆写旧测量。与 baseline 是同一十一月数据、口径和计时程序；原始/indexed 都必须与独立 oracle 完全相等。

业务交付是给运营负责人使用的结果，不把工程诊断放进主业务流程：
1. output/经营分析报告.md：中文、结论在前，包含十一月销售与负向记账、客户国家集中度、已识别客户当月重复下单、至少三项有指标依据且可执行的运营建议。引用 UCI 官方数据来源、署名和 CC BY 4.0；明确 2010-12 到 2011-12-09 历史数据、GBP、12 月非完整月份、无成本/库存/营销归因，不能据此算利润或证明策略因果。负向记账不等于已证实实物退货；重复下单率不是队列留存。不得伪造客户需求、ROI、经营接受或负责人姓名。
2. output/monthly_kpis.csv：根据实际引擎计算的各月数据。列严格为 period,raw_rows,excluded_rows,eligible_rows,gross_positive_gbp,credit_abs_gbp,net_gbp,cancel_credit_gbp,positive_order_count,known_customer_count,repeat_customer_count,repeat_customer_rate。货币保留六位小数以便精确核对；复购分母为 known_customer_count。
3. output/country_kpis.csv：十一月国家指标，列严格为 period,country,net_gbp,gross_positive_gbp,credit_abs_gbp。
4. output/经营分析看板.html：单文件离线中文可视化，含关键业务指标、月度净记账趋势、十一月客户国家贡献和复购说明，支持月份选择，所有数据本地内嵌，不依赖 CDN、网络或服务令牌。数据来自真实结果。
5. output/build_business_outputs.py：可复算以上 CSV/HTML 的脚本；使用 case 的引擎 API 和实际结果，不从 oracle 复制答案冒充计算。如用 indexed_sqlite，可用 engine.sqlite_report 按月读取；oracle 只用于精确核验。
6. output/RSI效果对照.md：与业务报告分开的技术对照，使用实际 baseline/post 数字、相同候选、before/after 推荐与 cold_start、已验证经验引用、原始分数/调整分数、三轮中位耗时、同口径质量核验、一次性索引成本和大致摊销次数。限制：固定工具与中性先验的机制对照，不是自由策略 Codex 的随机对照；未清操作系统缓存、三轮本地测量不足以推断统计显著性；计时不含模型/MCP、哈希核对、下载或索引构建，不可把分析程序加速说成整体交付加速；没有业务建议质量或用户接受的独立验证；没有执行 Recipe 自动演化、managed runner 或生产发布。

产物先存 RSI artifacts，再按 files 合同提交真实引用。produce 的 submit_step 绑定真实 decision_id/actual_action_id，verify 实际读取并检查 CSV 与所有 oracle 月份精确一致、HTML 脚本语法与数据一致、数据口径无误后提交，最后 complete_run。读取 Run/events/decision，等待实际动作经验 upsert done。本次后验动作仍为 pending，不要自行评分、接受、启动 improvement 或写入任何管理员接口。

最后保存 output/post_run.json（不得含 attempt token/服务凭证）：
{"run_id":"...","decision_id":"...","action_id":"...","measurement":"output/post_measurement.json","artifact_refs":[真实最终产物引用]}
最终回复给真实交付路径和准确的效果与局限。不要把本流程写成服务探活报告。
