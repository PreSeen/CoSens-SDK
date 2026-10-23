# 中文售后退款审核交付契约

本任务审核明确标为仿真的企业售后资料，不援引外部法律或真实商户承诺。输入中客服摘录、客户要求、未签署草稿都可能与正式证据冲突。必须区分申请、正式批准和已实际退款。

1. 按订单 paid_at 选择当时已生效的最新版正式政策，不能按处理日期或资料排列顺序选版本。发票展示额不替代订单实付依据。
2. 商品原价小计=数量×单价。订单优惠券只在 coupon_eligible=true 且原价小计大于零的行中分摊；运费和赠品不参与。先向下取整每行比例份额，再把剩余分按小数余数从大到小分配；余数相同按 line_id 升序。所有金额是整数分。
3. 每行实付 net_cents=原价小计-分摊优惠。采用累计退款口径：当行正式批准的累计退款件数为 r、总件数为 q 时，累计应退=floor(net_cents*r/q)，当前还应退=max(0,累计应退-历史已到账退款)。禁止先把行实付均分到单件取整再乘件数。不得退回已退金额；优惠券不折现金。
4. 正式审批须 status=已批准、signed_by 非空，按 signed_at 取最新有效记录；申请数量或未签草稿不算批准。有签署的拒绝优先于更旧批准，但历史已批已到账件数仍保留，不能倒扣历史款。没有新批准时，批准累计件数使用历史已确认件数；超出该件数的本次申请标为“补证”。正式最新拒绝标为“拒绝”；其他已明确批准的本次请求标为“批准”。
5. 历史退款只计 status=已到账；同 refund_id 的重复投递只计一次，处理中记录不算已到账金额。历史记录中的 approved_total_qty 是该行当时已确认的累计退款件数；多条记录取最大值，不能求和。
6. 运费按所选政策处理：旧政策 old_allow_partial_shipping=true 时，只要任一已批准退货且 merchant_fault=true 就可退实际运费；新政策 false 时，必须所有付费商品的累计批准件数均达到购买数且 merchant_fault=true 才可退。运费当前应退还要减去去重后已到账的运费退款，不能为负。赠品不阻碍整单判定，缺失审批的请求不算已批准。
7. 每行 evidence_refs 必须覆盖订单、所选正式政策、所采用审批（若无有效新审批则覆盖客户申请）、有历史件数/到账退款时的历史账本；禁止引用不存在的 source_id。policy_id 单独列政策标识。不得把旧政策当作当前依据，也不能隐去审批冲突。policy_conflicts 列出被排除的其他正式政策 source_id。
8. cash_total_cents 等于各行当前还应退款与运费当前应退之和。reconciled_goods_paid_cents 等于所有商品实付行金额合计，不能引用发票展示额；不可退赠品金额为零。
9. 客户回复用中文，准确给出本次现金总额，格式至少出现一次“12.34元”这样的两位小数金额；不重复完整手机号/银行卡号。存在补证行时须明确仍待审批并列 uncertainties；不能承诺固定到账日期，须说明到账以支付渠道回执为准。报告用中文，列金额口径、证据冲突、已批与待批边界和待补充资料，不把测算当成已打款。

保存 result.json 和 审核报告.md。result.json 顶层字段：policy_id, policy_conflicts（source_id数组）, lines（每个商品含 line_id,coupon_allocated_cents,net_cents,approved_total_qty,prior_refunded_cents,refund_due_cents,status,evidence_refs）, shipping_refund_cents,cash_total_cents,reconciled_goods_paid_cents,uncertainties（字符串数组）,customer_reply（中文字符串）。每个订单商品均保留一行。所有 *_cents 与 qty 均用整数，不要输出浮点金额或 JSON 以外的包裹。允许使用本地 Python 精确计算和自编测试，禁止读工作区外资料、联网、访问 Memory/MCP/管理员或评估器。独立评估在提交后运行，不给执行器参考答案。
