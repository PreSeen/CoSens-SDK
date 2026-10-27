# 国内网关与算法接口联调记录

日期：2026-10-27（Asia/Shanghai）。参考用户提供的 `FULL_CURL_TRANSCRIPT.md`、`friday-rsi/docs/api/gateway-authentication.md` 及实测网关 OpenAPI 1.0.0。

## 结论与身份契约

- 实际请求统一为 `https://friday-rsi-cn.stardust-ai.com/api` + 算法原路径，查询参数保留。
- SDK 只提供 Friday Key 的 `Authorization: Bearer`，不接受/发送 `X-User-Id` 或 `X_User_Id`；网关根据鉴权结果 accountId 注入。
- 无身份头的健康请求返回账号对应的 Memory project，Memory 写入和读取成功。单一 Key 的测试不能证明跨账号隔离；本次没有伪造身份头或冒充附件中的另一个用户。
- 原子主流程 **34 次 HTTP 200**；包装入口验证 **19 次 HTTP 200**。另有探测/持久化核对请求。原始 Key 未写入仓库或报告。
- SDK 增加公开 Memory、Recipe step、guidance、checkpoint、decision、trace 和显式管理员评估原子方法。
- 已补齐无 Run 召回：`GET /v1/recipes/active?task_family=...` + `POST /v1/memory/recall`。每个 turn 保存并复用组合结果，不创建 Run、不发送消息事件、不推进消息游标。两个读取不声称拥有服务端事务一致快照。

## 实测矩阵

下表路径均在 `/api` 后，详细请求 ID 在 JSON 记录中。

| 能力 | 接口 | 结果 |
| --- | --- | --- |
| 就绪状态 | `GET /health/ready` | 200，RSI/Memory ok |
| 实际契约 | `GET /openapi.json` | 200，版本 1.0.0 |
| 无 Run Recipe 查询 | `GET /v1/recipes/active` | matched 和 not_found 均验证 |
| Memory 写入 | `POST /v1/memory/ingest` | 200，返回 source/job |
| Memory 异步进度 | `GET /v1/memory/jobs/{id}` | committed |
| Memory 召回 / Wiki | `POST /v1/memory/recall`、`GET /v1/memory` | 200，可召回本次写入测试文本 |
| Recipe 初始化 | `POST /internal/admin/v1/recipes/bootstrap-bundle` | 独立测试任务族注册成功 |
| 输入/输出/证据 Artifact | `POST /v1/artifacts` | 200，返回引用及内容哈希 |
| Run 准备 | `POST /v1/runs/prepare` | ready；相同幂等键重发返回相同 Run |
| 冻结上下文 | `GET /v1/runs/{id}` | 与 prepare 配合通过 SDK Recipe/Memory 校验 |
| Recipe 节点 | `GET /v1/runs/{id}/recipe/steps/{node}` | prepare/produce/verify 三个节点均 200 |
| 推荐 | `POST /v1/runs/{id}/guidance` | 返回 decision 和 action |
| 消息事件 | `POST /v1/runs/{id}/session-events` | durable_ack；重复发送后 trace 中各 ID 仅一份 |
| 真实执行记录 | `POST /v1/runs/{id}/checkpoint` | saved；写入的实际动作为本地复算结果 |
| 决策/经验 | `GET /v1/runs/{id}/decisions/{decision_id}`、`GET /v1/memory/experiences/{id}` | upsert done，revision 1 |
| Run 提交 | `POST /v1/runs/{id}/submit` | completed |
| 反馈 | `POST /v1/runs/{id}/feedback` | recorded；反馈本身没有推进经验版本 |
| 决策评估 | `POST /internal/admin/v1/runs/{id}/decisions/{decision_id}/evaluations` | outcome done，revision 2、verified、value -1 |
| 历史经验 | `GET /v1/memory/experiences/{id}?revision=1` | 旧版本仍可读 |
| Recipe 评估 | `POST /internal/admin/v1/runs/{id}/recipe-evaluation` | recorded；本次未进一步证明 Recipe 经验消费/新版本发布 |
| 完整轨迹 | `GET /v1/runs/{id}/trace` | completed、session_events 和决策写入结果可核对 |

## 业务计算与测试数据

测试标识：`sdk-gateway-20261027T022516Z-9b326a`。

- Recipe：`recipe_d3a7108e99ad431d8e14a5cbf8042b90`
- 主流程 Run：`run_9152c7fa23da42eba4db79ea3e0d6dea`
- 经验：`exp_f5fe719e17da43a7a9ddf35a7aa73614`

采用附件 BATCH-CURL-A 合成输入，以本地 Decimal 复算 guidance 推荐的 `daily_aggregate`：订单净额 242.75，银行同日汇总 242.55，差额 -0.20。附件独立验收期望按转账引用匹配，银行到账 242.75，因此业务方案没有通过验收，真实评估记为 **-1**。接口流程通过不表示该错误策略得到正向评价。

本次写入的测试 Recipe、Memory、Artifacts、Runs 和经验会保留在此 Key 对应的远端数据中，未执行删除。测试任务族独立命名，但相同 Recipe 内容/动作语义的经验可能参与该账号后续召回。

## 三种 SDK 入口

除原子主流程外，真实网关加本地模型替身验证：

| 入口 | 结果 |
| --- | --- |
| 透明同步/异步 disable | passed；只查询 Recipe/Memory，run_id 为 null |
| 透明同步/异步 auto 未命中 | passed；只查询 Recipe/Memory，run_id 为 null |
| 透明同步/异步 auto 命中 | passed；Run completed |
| 显式 Agent run，mode=disable | passed；强制原子编排，Run completed |

模型决策及模型/Agent 结果由本地替身提供；本次验证的是 SDK 与真实网关的编排、注入、身份和提交契约，未调用真实大模型。没有将附件 `18124` 业务工具或 `18125` 演示 Agent 路径误拼到网关。

## 失败记录与未覆盖范围

第一次包装入口检查在同步 Memory recall 出现一次 30 秒读取超时，此前 Recipe 查询 200，尚未创建 Run 或执行模型。第二次执行全矩阵通过；未通过增加 SDK 自动重试或隐藏错误掩盖这次失败。需服务端结合日志排查偶发延迟。

本次未覆盖跨账号隔离、真实模型质量、远端演示工具、审批协调器、improvement/publish 和 initial-inputs 写入；不能根据这些测试声称全部后端能力已验收。

## 证据与复现

- [原子流程请求记录](testing/2026-10-27-workflow.json)
- [包装入口请求记录](testing/2026-10-27-wrappers.json)
- [首次超时记录](testing/2026-10-27-wrapper-timeout.json)
- [事件去重与经验结果核对](testing/2026-10-27-persistence.json)
- [原子流程验证脚本](../examples/validate_gateway.py)
- [包装入口验证脚本](../examples/validate_wrapped_gateway.py)

脚本读取 `RSI_API_KEY` 或使用隐藏输入，不执行附件中的 shell 命令、不持久化鉴权头。运行方法见 Python README。
