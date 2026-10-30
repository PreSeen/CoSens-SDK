# Python SDK 进阶与原子 API

基础安装和可运行示例见 [接入文档](../README.md)。以下是可选能力，普通透明接入只需 `native`、`api_key`、`mode`。

## 新任务工作流

`client.atoms` 和 `client.sync_atoms` 提供 Task、Execution、Revision、Attribution、Recipe Candidate 和 Verification 的原子方法。写入方法的 `data` 参数直接对应服务端请求体；归因、验收和 Recipe 等接口由 SDK 包装为 `{"data": ...}`。工作流需要宿主执行并提交真实产物，不能将网络超时视为远端未执行。

| SDK 方法 | 服务端路径 |
| --- | --- |
| `create_task(data=...)`, `get_task(id)`, `update_task_context(id, data=...)` | `POST /v1/tasks`, `GET /v1/tasks/{id}`, `POST /v1/tasks/{id}/context` |
| `get_artifact(id)` | `GET /v1/artifacts/{id}` |
| `start_task_analysis(id)`, `get_task_analysis(id)`, `resume_task_analysis(id)` | `/v1/tasks/{id}/analysis`, `/v1/task-analyses/{id}` 和其 `/resume` |
| `start_reconstruction(id)`, `start_execution(id)` | `/v1/tasks/{id}/reconstructions`, `/executions` |
| `get_execution(id)`, `submit_execution_result(id, data=...)`, `get_revision(id)` | `/v1/executions/{id}`、其 `/result`、`/v1/revisions/{id}` |
| `start_attribution(id, data=...)`, `get_attribution(id)`, `resume_attribution(id)`, `select_attribution(id, data=...)` | `/v1/revisions/{id}/attributions`、`/v1/attributions/{id}` 和其 `/resume`、`/selection` |
| `start_revision(id, data=...)`, `accept_revision(id, data=...)` | `/v1/revisions/{id}/revisions`、`/acceptance` |
| `start_recipe_candidate(id, data=...)`, `get_recipe_candidate(id)`, `resume_recipe_candidate(id)` | `/v1/tasks/{id}/recipe-candidates`、`/v1/recipe-candidates/{id}` 和其 `/resume` |
| `start_recipe_verification(id)`, `get_recipe_verification(id)` | `/v1/recipe-candidates/{id}/verifications`、`/v1/recipe-verifications/{id}` |

`TaskWorkflowRunner` 和 `CoSensClient.start()` 的原始实现及恢复契约在当前分支缺失，调用会明确报 `NotImplementedError`；请使用原子接口自行编排。

## 原子编排入口

`CoSensClient(api_key=...)` 是异步上下文管理器，`rsi.atoms` 不会替应用执行模型或工具。

所有 CoSens 原子接口（包括 `admin.atoms`）均发送 `X-Auth-Type: api_key` 与 `Authorization: Bearer <api_key>`；`Idempotency-Key` 仅在相应调用提供稳定幂等键时发送。显式提供的键必须为非空、最多 256 字符且不含控制字符；可选键传入空字符串会报错，不会静默省略。

| 方法 | 调用时机 | 网关 `/api` 后路径 |
| --- | --- | --- |
| `artifact(name=..., content=...)` | 有真实输入材料或输出文本时 | `POST /v1/artifacts` |
| `prepare(objective=..., key=...)` | 新任务，模型调用前；同任务重试复用 key | `POST /v1/runs/prepare` |
| `get_run(run_id)` | prepare 就绪后冻结 snapshot；或恢复对账 | `GET /v1/runs/{run_id}` |
| `session_events(run_id, events)` | 上报未 ACK 的事件；收到 durable_ack 才推进游标 | `POST /v1/runs/{run_id}/session-events` |
| `initial_inputs(run_id, artifact_ids)` | 补充实际初始材料 | `POST /v1/runs/{run_id}/initial-inputs` |
| `submit(run_id, data=..., key=...)` | 本地模型/工具完成后，提交实际输出 | `POST /v1/runs/{run_id}/submit` |
| `feedback(run_id, data=..., key=...)` | 有真实用户反馈或验收证据后 | `POST /v1/runs/{run_id}/feedback` |
| `improve(run_id, reason=..., key=..., feedback=..., model_api=...)` | 明确要求发起改进时，可绑定业务模型 API | `POST /v1/runs/{run_id}/improvements` |
| `get_improvement(job_id)` / `wait_improvement(job_id)` | 查询/有界轮询异步改进 | `GET /v1/improvements/{job_id}` |
| `resume_improvement(job_id, model_api=...)` | 恢复原任务；可补充业务模型配置或轮换 Key | `POST /v1/improvements/{job_id}/resume` |
| `confirm_attribution(job_id, candidate_id=..., candidate_sha256=...)` | 归因候选待确认时，选择供后续验证的假设 | `POST /v1/improvements/{job_id}/attribution/confirm` |
| `admin.atoms.publish(job_id, expected_revision=..., reason=...)` | 显式批准发布时，须由外部网关实施管理员授权 | `POST /internal/admin/v1/improvements/{job_id}/publish` |

`prepare` 返回 `processing` 时，使用同一个 key 有界轮询；只有 `ready` 才进入模型调用。不要将一次请求超时当成失败后创建新任务的依据。三种入口的详细接口时机见 [设计文档](2026-10-26-cosens-python-sdk-design.md)。

改进任务可显式使用调用方的业务模型 API。模型服务须支持 OpenAI 兼容 Chat Completions 和 function tools：

```python
model_api = {
    "base_url": "https://business-model.example/v1",
    "model": "business-model-name",
    "api_key": business_model_key,
}
job = await client.atoms.improve(
    run_id, reason="分析遗漏并改进方法", key="improvement-1", model_api=model_api,
)
# 旧任务缺少运行时，或需要补充/轮换模型 Key 时，恢复原任务：
await client.atoms.resume_improvement(job.job_id, model_api=model_api)
```

`model_api` 是独立于 Friday Key 的业务模型连接；可选 `temperature`、`max_tokens`。仅显式提供时才发送，包装 native client 不会自动上传其凭证。服务端用该连接完成归因、候选生成、重跑及产物评审；须部署支持该参数的服务端及 bubblewrap 隔离工具。模型 Key 不进入任务报告，SDK 对网关异常中的模型 Key 做脱敏。模型/地址/生成参数在实验冻结后不能替换，但可以在可恢复状态轮换 Key。不传新参数保持原请求格式。

`wait_improvement` 遇到 `awaiting_attribution_review` 会返回；读取 `job.report.attribution_review`，再调用 `confirm_attribution` 传入候选 ID 及冻结的 `candidate_sha256`。`candidate_id="none"` 表示拒绝所有候选。确认后仍需重跑、有效验收标准及独立验证任务，验证通过后才能显式发布；不会因为模型请求成功而自动发布。没有已校准标准编译器时，应提交明确的 `acceptance.checks` 或反馈 `acceptance_checks`。

## Session、工具和恢复

透明方式的 `session` 为高级可选项：不传时捕获本次请求可见的用户文本、回复及已适配工具事件，不能读取应用未提供的历史。外部 Session 需提供稳定的 `session_id` 和 `get_items()`。建议模型调用前写入本次用户消息，保持稳定消息 ID；后写入的新 ID 无法证明与已合成事件相同。用 `client.cosens.scope(session_id="...", turn_id="...")` 显式划定一个逻辑 turn，工具循环复用它。

显式 Agent 的 Session 交给 Agents SDK 管理；不传 Session 时会提交最终结果，但没有 Session 聊天事件可增量上报。可运行的 [显式示例](../examples/02_explicit_run.py) 使用持久化 `SQLiteSession`。恢复需使用原 session_id、Session 数据库和 CoSens 状态文件。

原生 Agent 工具审批中断时不会提交最终结果。通过 `await client.pending_state(agent, turn_id)` 取得原生 `RunState`，按 Agents SDK `approve()`/`reject()` 后，调用 `await client.resume(agent, turn_id, native_state, session=session)` 继续。`input_artifacts=[{"name": "...", "content": "..."}]` 上传真实输入文本；`final_output` 含 `files` 列表时可上传实际输出文件。

## 恢复与服务端边界

Session 事件只在收到 `durable_ack` 后推进本地游标。失败后可查 `client.cosens.get_turn(turn_id)`；原生执行已完成但提交回执不明时用 `client.cosens.reconcile(turn_id)` 对账（异步客户端使用 `await`），不重新执行模型或工具。Agent 若在 `run()` 中传入 Session，对账也须显式 `await client.cosens.reconcile(turn_id, session=session)`；重启后先恢复原 session_id 和 Session 数据库，否则无法补报未 ACK 的历史。原生执行结果不明时抛 `RSIRecoveryRequired`，由宿主确认实际执行情况。透明调用的重试及工具续接必须保持 turn 创建时的 `mode`；模式变化或旧记录缺少模式时，SDK 会在模型调用和远端写入前拒绝隐式恢复。请持久化 `state_path`；丢失本地状态后，当前服务端不能保证跨 Run 的消息全局去重。

网关 OpenAPI 当前标识为 1.0.0；以本次 curl 附件、网关实际返回和联调报告为准。审批协调器 `/v1/invocations` 仍未确认，透明 `approval` 命中时保持明确报错。单元测试使用模拟 HTTP；真实网关联调使用独立测试标识并写入合成测试数据，模型执行用本地替身验证编排，未调用真实模型或银行系统。详细时机与限制见 [设计文档](2026-10-26-cosens-python-sdk-design.md)。

## Recipe、Memory 与决策接口

`client.atoms.list_recipe_candidates(limit=20, cursor=None, task_id=None, status=None)`
列出当前用户的 Task 工作流 Recipe 候选摘要，调用 `GET /v1/recipe-candidates`；
同步调用使用 `client.sync_atoms.list_recipe_candidates(...)`。`limit` 为 1–100，
`status` 可为 `queued`、`running`、`awaiting_runner`、`candidate` 或 `failed`。
保持筛选条件不变，使用响应的 `next_cursor` 翻页，直到 `has_more` 为 false。
列表不包含旧 Run 的 active Recipe，完整内容需通过候选详情接口获取。
用户身份由网关根据 API Key 注入；接口可用性取决于网关是否转发该路径及服务是否部署。

普通原子接口（`atoms` 异步，`sync_atoms` 提供新增接口的同步形式）：

| SDK 方法 | 网关 `/api` 后的路径 |
| --- | --- |
| `get_active_recipe(task_family=...)` | `GET /v1/recipes/active?task_family=...` |
| `memory_ingest(content=..., request_key=..., source_id=..., expected_revision=..., source_kind=..., durable=...)` | `POST /v1/memory/ingest` |
| `memory_recall(query=..., token_budget=1000)` | `POST /v1/memory/recall`；预算 1–8000 |
| `get_memory_items(limit=200)` | `GET /v1/memory/items?limit=...`；limit 1–500 |
| `get_memory_job(job_id)` | `GET /v1/memory/jobs/{job_id}` |
| `get_memory_experience(experience_id, revision=...)` | `GET /v1/memory/experiences/{experience_id}` |
| `get_memory()` | `GET /v1/memory` |
| `get_recipe_step(run_id, node_id)` | `GET /v1/runs/{run_id}/recipe/steps/{node_id}` |
| `guidance(run_id, data=...)` | `POST /v1/runs/{run_id}/guidance` |
| `checkpoint(run_id, data=...)` | `POST /v1/runs/{run_id}/checkpoint` |
| `get_decision(run_id, decision_id)` | `GET /v1/runs/{run_id}/decisions/{decision_id}` |
| `get_trace(run_id)` | `GET /v1/runs/{run_id}/trace` |
| `next_step(run_id)` | `POST /v1/runs/{run_id}/steps/next` |
| `submit_step(run_id, attempt_id, attempt_token=..., outputs=..., decision_id=..., actual_action_id=...)` | `POST /v1/runs/{run_id}/steps/{attempt_id}/submit` |
| `complete_guided(run_id)` | `POST /v1/runs/{run_id}/complete` |
| `get_events(run_id)` | `GET /v1/runs/{run_id}/events` |

`memory_ingest` 为兼容既有调用，SDK 仍显式发送 `source_kind="decision"`、`durable=True`；原始服务省略字段时的默认值分别是 `message`、`false`。需要原始服务默认语义时显式传 `source_kind="message", durable=False`。新增来源可省略 `expected_revision`（服务默认 0）；更新来源时传入 `source_id` 和当前 revision。`memory_ingest` 返回 `pending` 时使用原 `job_id` 查询至 `committed` 或 `failed`。`prepare(..., execution_mode="guided")` 启用 guided；`managed` 被客户端拒绝。guided 步骤的 `attempt_id`/`attempt_token` 应持久保存；原生动作结果未知时先对账，不能重放动作。`get_events` 与 `get_trace` 是不同接口，`complete_guided` 不代替 assist 的 `submit`。

CN 已验证：`next_step` 的 `step_ready` 回执在 `node.output_contract` 声明输出端口。artifact 端口应传 `outputs={"files": [{"artifact_id": ..., "name": ..., "content_hash": ...}]}`，引用上传回执；单独 ID 不满足该端口。`submit_step` 的 HTTP 200 仍可能返回 `repair_required`，只有 `step_passed` 表示步骤通过。全流程完成后 `complete_guided` 返回 `completed`；`get_events` 返回含 `items` 列表的对象。

`admin.atoms` 新增显式调用：`bootstrap_bundle`、`evaluate_decision`、`evaluate_recipe`，对应附件的 Recipe 注册、决策评估、Recipe 评估接口。它们不会由透明包装自动触发；调用者须提交实际执行和评估证据。`feedback` 不等于经验已经 verified，异步写入需轮询 Job/经验版本确认。

`recall_context` 顺序读取 Recipe 和 Memory 后冻结本地结果，服务端未承诺两个独立读取拥有同一个事务快照。`disable` 使用 `general` 任务族；`auto` 未命中时使用分类结果的任务族。Recipe `not_found` 或 Memory 空列表是合法结果，HTTP 错误和缺失字段仍阻止业务模型调用。

## 复现网关联调

先由运行环境注入 `RSI_API_KEY`（没有设置时脚本隐藏输入 Key），在仓库根目录运行：

两个诊断脚本均支持 `COSENS_REGION=cn`（默认）或 `us`，网关分别为 `https://friday-rsi-cn.stardust-ai.com/api`、`https://rsi.cosens.ai/api`。它们不加载 `.env` 或选择 `RSI_API_KEY_US`；请将目标区域的 Key 注入进程的 `RSI_API_KEY`。三种可直接运行的模型示例会读取 `.env` 并选择区域 Key，详见 [区域配置](../README.md#国内区与美国区测试)。

```bash
# 只读验证：健康状态和无 Run 召回
python python/examples/validate_gateway.py --output /tmp/cosens-readonly.json

# 会写入合成 Recipe、Memory、Run 和评估记录
python python/examples/validate_gateway.py --write \
  --transcript /path/to/FULL_CURL_TRANSCRIPT.md --output /tmp/cosens-workflow.json

# 真实网关 + 本地模型/Agent 替身；会创建测试 Run，需要 agents 可选依赖
python python/examples/validate_wrapped_gateway.py --output /tmp/cosens-wrapped.json
```

## 文档上传

Python 异步 `client.atoms` 和同步 `client.sync_atoms` 支持以下方法：

| 方法 | 请求 |
| --- | --- |
| `upload_document(filename=..., mime_type=..., content=..., **metadata)` | 普通 multipart 上传 |
| `get_document_status(document_ids)` | 查询非空文档 ID 列表 |

```python
import asyncio
import os
from pathlib import Path
from cosens import CoSensClient

async def main():
    os.environ["COSENS_REGION"] = "cn"
    async with CoSensClient(api_key=os.environ["RSI_API_KEY"]) as client:
        content = Path("notes.txt").read_bytes()
        receipt = await client.atoms.upload_document(
            filename="notes.txt", mime_type="text/plain", content=content, wait_for_processing=False,
        )
        # 持久化回执并检查业务状态；HTTP 200 不等于文件已完成处理。
        if receipt.get("document_id"):
            status = await client.atoms.get_document_status([receipt["document_id"]])
            print(status)

asyncio.run(main())
```

同步宿主改用 `client.sync_atoms` 并去掉方法调用的 `await`。上传结果不明时应先对账，不应盲目重新上传。

算法服务当前版本已移除一次性上传 grant 路由，因此 SDK 不提供 grant 上传。SDK 不接受用户身份覆盖；Key 来自构造参数或进程环境 `RSI_API_KEY` / `COSENS_API_KEY`，SDK 本身不自动加载 `.env`。实际网关支持情况需联调确认。
