# Python SDK 进阶与原子 API

基础安装和可运行示例见 [接入文档](../README.md)。以下是可选能力，普通透明接入只需 `native`、`api_key`、`mode`。

## 原子编排入口

`CoSensClient(api_key=...)` 是异步上下文管理器，`rsi.atoms` 不会替应用执行模型或工具。

| 方法 | 调用时机 | 网关 `/api` 后路径 |
| --- | --- | --- |
| `artifact(name=..., content=...)` | 有真实输入材料或输出文本时 | `POST /v1/artifacts` |
| `prepare(objective=..., key=...)` | 新任务，模型调用前；同任务重试复用 key | `POST /v1/runs/prepare` |
| `get_run(run_id)` | prepare 就绪后冻结 snapshot；或恢复对账 | `GET /v1/runs/{run_id}` |
| `session_events(run_id, events)` | 上报未 ACK 的事件；收到 durable_ack 才推进游标 | `POST /v1/runs/{run_id}/session-events` |
| `initial_inputs(run_id, artifact_ids)` | 补充实际初始材料 | `POST /v1/runs/{run_id}/initial-inputs` |
| `submit(run_id, data=..., key=...)` | 本地模型/工具完成后，提交实际输出 | `POST /v1/runs/{run_id}/submit` |
| `feedback(run_id, data=..., key=...)` | 有真实用户反馈或验收证据后 | `POST /v1/runs/{run_id}/feedback` |
| `improve(run_id, reason=..., key=...)` | 明确要求发起改进时 | `POST /v1/runs/{run_id}/improvements` |
| `get_improvement(job_id)` / `wait_improvement(job_id)` | 查询/有界轮询异步改进 | `GET /v1/improvements/{job_id}` |
| `resume_improvement(job_id)` | 确认任务可恢复后 | `POST /v1/improvements/{job_id}/resume` |
| `admin.atoms.publish(job_id, expected_revision=..., reason=...)` | 显式批准发布时，由服务端校验权限 | `POST /internal/admin/v1/improvements/{job_id}/publish` |

`prepare` 返回 `processing` 时，使用同一个 key 有界轮询；只有 `ready` 才进入模型调用。不要将一次请求超时当成失败后创建新任务的依据。三种入口的详细接口时机见 [设计文档](2026-10-26-cosens-python-sdk-design.md)。

## Session、工具和恢复

透明方式的 `session` 为高级可选项：不传时捕获本次请求可见的用户文本、回复及已适配工具事件，不能读取应用未提供的历史。外部 Session 需提供稳定的 `session_id` 和 `get_items()`。建议模型调用前写入本次用户消息，保持稳定消息 ID；后写入的新 ID 无法证明与已合成事件相同。用 `client.cosens.scope(session_id="...", turn_id="...")` 显式划定一个逻辑 turn，工具循环复用它。

显式 Agent 的 Session 交给 Agents SDK 管理；不传 Session 时会提交最终结果，但没有 Session 聊天事件可增量上报。可运行的 [显式示例](../examples/02_explicit_run.py) 使用持久化 `SQLiteSession`。恢复需使用原 session_id、Session 数据库和 CoSens 状态文件。

原生 Agent 工具审批中断时不会提交最终结果。通过 `await client.pending_state(agent, turn_id)` 取得原生 `RunState`，按 Agents SDK `approve()`/`reject()` 后，调用 `await client.resume(agent, turn_id, native_state, session=session)` 继续。`input_artifacts=[{"name": "...", "content": "..."}]` 上传真实输入文本；`final_output` 含 `files` 列表时可上传实际输出文件。

## 恢复与服务端边界

Session 事件只在收到 `durable_ack` 后推进本地游标。失败后可查 `client.cosens.get_turn(turn_id)`；原生执行已完成但提交回执不明时用 `client.cosens.reconcile(turn_id)` 对账（异步客户端使用 `await`），不重新执行模型或工具。Agent 若在 `run()` 中传入 Session，对账也须显式 `await client.cosens.reconcile(turn_id, session=session)`；重启后先恢复原 session_id 和 Session 数据库，否则无法补报未 ACK 的历史。原生执行结果不明时抛 `RSIRecoveryRequired`，由宿主确认实际执行情况。透明调用的重试及工具续接必须保持 turn 创建时的 `mode`；模式变化或旧记录缺少模式时，SDK 会在模型调用和远端写入前拒绝隐式恢复。请持久化 `state_path`；丢失本地状态后，当前服务端不能保证跨 Run 的消息全局去重。

网关 OpenAPI 当前标识为 1.0.0；以本次 curl 附件、网关实际返回和联调报告为准。审批协调器 `/v1/invocations` 仍未确认，透明 `approval` 命中时保持明确报错。单元测试使用模拟 HTTP；真实网关联调使用独立测试标识并写入合成测试数据，模型执行用本地替身验证编排，未调用真实模型或银行系统。详细时机与限制见 [设计文档](2026-10-26-cosens-python-sdk-design.md)。

## Recipe、Memory 与决策接口

普通原子接口（`atoms` 异步，`sync_atoms` 提供新增接口的同步形式）：

| SDK 方法 | 网关 `/api` 后的路径 |
| --- | --- |
| `get_active_recipe(task_family=...)` | `GET /v1/recipes/active?task_family=...` |
| `memory_ingest(content=..., request_key=...)` | `POST /v1/memory/ingest` |
| `memory_recall(query=..., token_budget=1000)` | `POST /v1/memory/recall` |
| `get_memory_job(job_id)` | `GET /v1/memory/jobs/{job_id}` |
| `get_memory_experience(experience_id, revision=...)` | `GET /v1/memory/experiences/{experience_id}` |
| `get_memory()` | `GET /v1/memory` |
| `get_recipe_step(run_id, node_id)` | `GET /v1/runs/{run_id}/recipe/steps/{node_id}` |
| `guidance(run_id, data=...)` | `POST /v1/runs/{run_id}/guidance` |
| `checkpoint(run_id, data=...)` | `POST /v1/runs/{run_id}/checkpoint` |
| `get_decision(run_id, decision_id)` | `GET /v1/runs/{run_id}/decisions/{decision_id}` |
| `get_trace(run_id)` | `GET /v1/runs/{run_id}/trace` |

`admin.atoms` 新增显式调用：`bootstrap_bundle`、`evaluate_decision`、`evaluate_recipe`，对应附件的 Recipe 注册、决策评估、Recipe 评估接口。它们不会由透明包装自动触发；调用者须提交实际执行和评估证据。`feedback` 不等于经验已经 verified，异步写入需轮询 Job/经验版本确认。

`recall_context` 顺序读取 Recipe 和 Memory 后冻结本地结果，服务端未承诺两个独立读取拥有同一个事务快照。`disable` 使用 `general` 任务族；`auto` 未命中时使用分类结果的任务族。Recipe `not_found` 或 Memory 空列表是合法结果，HTTP 错误和缺失字段仍阻止业务模型调用。

## 复现网关联调

先由运行环境注入 `RSI_API_KEY`（没有设置时脚本隐藏输入 Key），在仓库根目录运行：

```bash
# 只读验证：健康状态和无 Run 召回
python python/examples/validate_gateway.py --output /tmp/cosens-readonly.json

# 会写入合成 Recipe、Memory、Run 和评估记录
python python/examples/validate_gateway.py --write \
  --transcript /path/to/FULL_CURL_TRANSCRIPT.md --output /tmp/cosens-workflow.json

# 真实网关 + 本地模型/Agent 替身；会创建测试 Run，需要 agents 可选依赖
python python/examples/validate_wrapped_gateway.py --output /tmp/cosens-wrapped.json
```
