# 支付回调重复与乱序：修复交付需求

这是独立的支付业务仿真模块，不接真实支付、不发送订单。任务是修复已有 Python 实现，由本地 Codex 真实编写补丁；不是预先写好两份实现后切换。

故障：网关至少投递一次，同一笔支付会有不同 event_id 的重试，退款通知可能先于支付通知到达。客服观察到重复确认、退款被迟到的支付覆盖、重启后重复发货。

公开接口保持：`new_store()` 返回 JSON 可序列化字典；`apply_event(store, event)` 原地更新并返回该订单视图；`order_view(store, order_id)` 返回 `status`, `captured_cents`, `refunded_cents`, `fulfillments` 四字段。禁止 I/O、全局状态或依赖第三方包。

所有事件带非空字符串 `event_id`, `order_id`, `kind`。kind 为 `capture`, `refund`, `failed`。capture/refund 还带非空 `payment_id` 和正整数 `amount_cents`；refund 带非空 `refund_id`。bool 不是有效金额；failed 不要求金额和 payment_id。非法事件抛 ValueError，且 store 必须逐字内容不变，包括去重记录。

业务约束（训练与留出任务都可看到以下全部约束）：

1. 金额用整数分，累计捕获与退款，禁止浮点。多个 payment_id 可以属于一个订单。
2. capture 的业务唯一键是 `(order_id,payment_id)`。同键、同金额不同 event_id 的重试不重复记账；同键金额冲突拒绝且无副作用。
3. refund 的唯一键是 `(order_id,payment_id,refund_id)`。同金额业务重试幂等；不同金额冲突拒绝且无副作用。不同订单/支付可以复用 refund_id。
4. 相同 event_id、相同事件语义的投递幂等；相同 event_id 被用于不同语义（订单、kind、业务键或金额变化）必须拒绝且无副作用。
5. 退款可以先到：保存待归并退款，此时未捕获金额为 0、已退款金额为 0，订单 pending；对应 capture 到达后按同一 payment 归并。
6. 同一 payment 已知退款总额不能超过其 capture 金额；导致超额的事件拒绝且无副作用。先到退款再来金额不足的 capture 同样拒绝，但不删除已存在的待归并退款；可用新的合法 capture 重试。
7. `captured_cents` 为不同有效 capture 之和，`refunded_cents` 只计入已存在对应 capture 的退款。退款不能抵扣其他 payment 的金额。
8. 无有效 capture 为 pending；有 capture 且无有效退款为 paid；部分退款为 partially_refunded；全额退款为 refunded。failed 仅表示一次支付尝试失败，不能撤销任何已确认 capture 或退款，未成功的订单也保持 pending。
9. `fulfillments` 是逻辑出库信号累计次数：首次捕获后净金额大于0时变为1，之后永不增加，跨支付、重试、退款、失败与重启都如此。如果首个 capture 到达时已经被先到退款全额抵消，则不产生出库信号；后续另一笔 capture 让净额变正可以产生首次信号。无并发要求。
10. store 经 `json.dumps` / `json.loads` 往返后，上述状态和去重信息全部有效。查询不存在的订单返回 pending/0/0/0，查询不得修改 store。

先阅读已有测试与实现。可以修改目标目录内的 payment.py、添加自己的回归测试与中文修复说明；不要改需求、既有测试和外部评估器。不要只为通过已知样例而硬编码 ID。独立验收在提交后运行，以同一公开契约评价真实补丁，不向模型提供留出样例或答案。
