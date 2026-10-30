# CoSens 任务工作流 SDK 开发契约（Python / TypeScript）

更新时间：2026-10-29。任务工作流主体依据用户提供的 `RSI_WORKFLOW_API(2).md` 整理；第 8 节依据新版 `RSI_WORKFLOW_API(3).md` 补充。附件是**下游服务契约资料**；其中“SDK 应实现”等文字用于定义待开发接口，不代表服务已部署到当前 CN/US 网关，也不覆盖本项目此前由用户明确指定的认证规则。新版附件称其对应服务端源码提交 `8daf1b7`，但静态 `RSI_WORKFLOW_OPENAPI.json` 未随附件提供；本文以附件的 HTTP 表和 JSON 示例为准，待取得目标网关 OpenAPI 与实际回执后再确认部署契约。

## 1. 范围与兼容

新流程为 `Task → Execution → Revision → Attribution/Selection → 新 Execution/Revision → Acceptance → Recipe Candidate → Verification`。宿主负责 Agent、工具、审批和真实产物；服务保存冻结请求、记录结果、生成归因和候选。`submit_execution` 只记录一次执行，不等于用户验收。任务内修订不调用旧 `/v1/improvements`。

当前 Python `cosens(OpenAI)` / `cosens(Agent)` 和 TypeScript 同名包装器仍基于旧 `/v1/runs`、Memory、Recipe 协议。新任务协议增量提供 `client.atoms`、显式 `client.start` 和 `cosens(Runner)`；原有行为和返回类型保持不变。新协议 `disable` 完全不向 RSI 发请求；旧包装器中的 `disable` 仍保留旧设计的独立召回/显式 Run 语义。新 Runner 包装为显式 opt-in，且只在一次原生 turn 上自动提交结果；归因、修订、验收、Recipe 和验证由控制入口启动。跨阶段的自动宿主执行与持久化协调器仍未实现。

## 2. 传输与身份

公共 SDK 仍使用内置 CN/US 区域表和服务相对路径 `/v1/...`；传输层生成 `/api/v1/...`。每个网关请求同时发送：

```http
X-Auth-Type: api_key
Authorization: Bearer <api_key>
```

Key 不进入 URL、业务 JSON、日志或持久化状态。现有 SDK 不发送 `X-User-ID`。新版附件称原始服务对**包括健康和 OpenAPI 在内的所有 HTTP 路由**要求非空 `X-User-ID`，但它只是 Memory 用户选择字段，并非认证凭据；原始服务自身不校验 Bearer，RSI 主体仍固定为 `local-codex`。这描述的是服务内部边界，不能据此替换用户指定的两个网关认证头。接入新版服务前须由网关确认如何从已认证 Key 注入并稳定传递 `X-User-ID`，尤其是一次性上传的两步请求；不能把客户端任意填写的用户 ID 当成授权依据。当前 SDK 的不发送用户 ID 行为保持不变，待网关联调再决定是否需要改动。生产网关是否已转发新路由也需单独验收。Python/TypeScript 都拒绝外部绝对 URL 和 HTTP 重定向。

## 3. 公开原子方法

Python：`client.atoms.<snake_case>`；同步宿主可用 `client.sync_atoms.<snake_case>`。TypeScript：`client.atoms.<camelCase>`。每次调用只发一个请求，不自动上传工件、运行 Agent、轮询或重放副作用。**下表仅是上一版 Task 工作流原子接口**，均已在两种语言中加入；`artifact` 是既有上传方法。新版附件扩展的 Run、Memory、文档上传及内部/管理接口见第 8 节，其中未实现项不可视为当前 SDK 能力。

| 阶段 | Python / TypeScript | 请求 |
| --- | --- | --- |
| 工件 | `artifact` / `artifact`；`get_artifact` / `getArtifact` | `POST /v1/artifacts`；`GET /v1/artifacts/{id}` |
| 任务 | `create_task` / `createTask`；`get_task` / `getTask`；`supplement_task_context` / `supplementTaskContext` | `POST /v1/tasks`；`GET /v1/tasks/{id}`；`POST /v1/tasks/{id}/context` |
| 外部分析 | `start_external_analysis` / `startExternalAnalysis`；`get_external_analysis` / `getExternalAnalysis`；`resume_external_analysis` / `resumeExternalAnalysis` | `POST /v1/tasks/{id}/analysis`；`GET /v1/task-analyses/{id}`；`POST .../resume` |
| 宿主执行 | `start_execution` / `startExecution`；`get_execution` / `getExecution`；`submit_execution` / `submitExecution` | `POST /v1/tasks/{id}/executions`；`GET /v1/executions/{id}`；`POST /v1/executions/{id}/result` |
| 外部产物兼容 | `start_reconstruction` / `startReconstruction` | `POST /v1/tasks/{id}/reconstructions`，仅外部产物任务；与统一执行入口共用幂等键空间 |
| 版本归因 | `get_revision` / `getRevision`；`start_attribution` / `startAttribution`；`get_attribution` / `getAttribution`；`resume_attribution` / `resumeAttribution` | `GET /v1/revisions/{id}`；`POST /v1/revisions/{id}/attributions`；`GET /v1/attributions/{id}`；`POST .../resume` |
| 原因与修订 | `select_cause` / `selectCause`；`start_revision` / `startRevision` | `POST /v1/attributions/{id}/selection`；`POST /v1/revisions/{id}/revisions` |
| 验收 | `accept_revision` / `acceptRevision` | `POST /v1/revisions/{id}/acceptance` |
| Recipe 候选 | `start_recipe_candidate` / `startRecipeCandidate`；`get_recipe_candidate` / `getRecipeCandidate`；`resume_recipe_candidate` / `resumeRecipeCandidate` | `POST /v1/tasks/{id}/recipe-candidates`；`GET /v1/recipe-candidates/{id}`；`POST .../resume` |
| 复现 | `start_verification` / `startVerification`；`get_verification` / `getVerification` | `POST /v1/recipe-candidates/{id}/verifications`；`GET /v1/recipe-verifications/{id}` |

Python 以关键字参数传值，如 `create_task(source="new_task", objective="...", artifacts=[], key="stable-key")`。TypeScript 用对象和 camelCase，如 `createTask({source:'new_task', objective:'...', artifacts:[], key:'stable-key'})`。`submitExecution` 使用 `requestSha256`、`traceCoverage`；`supplementTaskContext` 使用 `expectedContextVersion`。所有回执保持服务端字段名，不伪造类型或状态。

异步作业可用 Python `wait_workflow_job(kind, job_id, timeout=300)` / TypeScript `waitWorkflowJob(kind, id, {timeout:300})` 对已知 ID 有界轮询；Python 同步 atoms 也提供同名方法。`kind` 为 `analysis`、`attribution` 或 `recipe_candidate`。遇到 `awaiting_runner`、成功或失败状态会返回当前回执；超时抛错并保留 ID，不创建新作业，也不自动调用 `/resume`。

## 4. 请求和状态约束

- `artifact` 上传非空 UTF-8 文本，正文最多 1,000,000 字节；服务返回 `artifact_id`、`content_hash`、`size_bytes`。必须保存服务端哈希，不能自行替换。上传无幂等键，丢回执时先对账，不盲目重复上传。
- `create_task` 的 `source` 为 `new_task` / `external_artifact`；`objective` 可省略，提供时非空且 ≤20,000 字符；`artifacts` 最多 100 项，元素为 ID 或带 `artifact_id` / `role` 的对象；`self_contained` 默认 false。外部产物至少一个 `submitted_output`。`supplement_task_context` 必须带当前 `expected_context_version`；旧执行的冻结请求不受后续上下文变更影响。
- 分析只用于外部产物。状态为 `queued/running/awaiting_runner/completed/failed`；推测的目标须由用户确认，再通过 context 写入。`resume` 仅用于 `awaiting_runner`。
- 创建执行返回 `execution_id`、`request_sha256`、`request`、`awaiting_execution`。宿主从冻结请求读取材料，在本地执行一次。`submit_execution` 的 `outcome` 为 `completed/failed/cancelled`；完成须有 `submitted_output` 工件；`events` 最多 200 项且 ID 唯一；`trace_coverage` 为 `full/partial/unknown`。结果 JSON 最大 1 MB。失败或取消不创建 Revision。
- 归因请求体为 `{data:{feedback?}}`，反馈可省略，提供时非空且 ≤4,000 字符。状态为 `queued/running/awaiting_runner/awaiting_selection/inconclusive/selected/failed`。只有 `awaiting_selection` 或 `inconclusive` 可提交原因；`select_cause` 必须携带 `candidate_sha256`，并在 `candidate_id` 与 `custom_reason` 中二选一。原因是用户选定假设，并非已证实根因。
- `start_revision` 的 `{data:{attribution_id}}` 必须引用该父 Revision 上已选择的归因，返回一个新的宿主 Execution。多轮继续以新 Revision 为父版本。
- `accept_revision` 的 `{data:{artifact_hashes:{id:content_hash}}}` 需覆盖该版本**全部输出**，且必须由真实用户明确接受。Recipe 只接受已验收 Revision。`include_memory` 默认 false；启用时服务负责来源校验，不自动写回共享 Memory。
- 验证返回新执行请求；宿主必须重新运行并上传**新的 artifact ID**。`GET verification` 的结果分基于文件名和内容哈希精确一致，流程分基于宿主上报步骤 ID，均不能当成独立业务质量证明。

## 5. 稳定键、恢复和并发

创建任务、分析、执行、归因、修订、Recipe 候选和验证必须使用非空、≤256 字符且不含控制字符的 `Idempotency-Key`。旧 Run 接口的可选键只有未提供时才省略，显式空字符串会在发送前报错。同操作同正文重试原键；变更正文用新键。结果提交、原因选择、验收用内容冻结语义，丢响应时重发**完全相同正文**。保存 `task_id/execution_id/revision_id/attribution_id/recipe_candidate_id/verification_id`、冻结请求与 `request_sha256`、上传回执和结果正文。已派发 Agent 的副作用未知时停止并对账；不得自动重跑。异步作业只轮询已知 ID，设置总期限；`running` 中断不自动 `resume`。同 Session 的执行顺序由宿主管理。

HTTP 领域错误通常为 `{error:{code,message}}`，校验错误使用 `detail`。常见状态码 403/404/409/422/503。业务 HTTP 200 也须核对 `status`。SDK 原子层透传成功 JSON，HTTP/传输错误走既有异常类型。

## 6. 手动编排示例

```python
source = await client.atoms.artifact(name="input.txt", content=authorized_text)
task = await client.atoms.create_task(source="new_task", objective=objective,
    artifacts=[source.artifact_id], key=stable_task_key)
execution = await client.atoms.start_execution(task.task_id, key=stable_execution_key)
native_output = await application_agent(execution.request)  # 仅宿主执行
output = await client.atoms.artifact(name="answer.txt", content=native_output)
receipt = await client.atoms.submit_execution(execution.execution_id,
    request_sha256=execution.request_sha256, outcome="completed",
    artifacts=[{"artifact_id": output.artifact_id, "role": "submitted_output"}],
    events=observed_events, trace_coverage="partial")
# 用户决定是否归因、修订或验收；此处不自动调用这些接口。
```

```typescript
const input = await client.atoms.artifact({ name: 'input.txt', content: authorizedText });
const task = await client.atoms.createTask({ source: 'new_task', objective,
  artifacts: [input.artifact_id], key: stableTaskKey });
const execution = await client.atoms.startExecution(task.task_id, { key: stableExecutionKey });
const nativeOutput = await applicationAgent(execution.request);
const output = await client.atoms.artifact({ name: 'answer.txt', content: nativeOutput });
const receipt = await client.atoms.submitExecution(execution.execution_id, {
  requestSha256: execution.request_sha256, outcome: 'completed',
  artifacts: [{ artifact_id: output.artifact_id, role: 'submitted_output' }],
  events: observedEvents, traceCoverage: 'partial',
});
```

### 显式 start

两种语言的 `client.start(snapshot, phase=..., mode=..., request_key=...)` / `client.start(snapshot, {phase,mode,requestKey})` 支持 `prepare/analyze/attribute/revise/recipe/verify`。`prepare` 用稳定派生键先创建任务，再在 `ready` 时创建执行请求；`needs_context` 只返回 Task，宿主补材料后用已知 `task_id` 继续。其他阶段各发一个创建请求，返回原始作业回执。`start` 不运行 Agent、不提交结果，也不自动验收。`disable` 立即返回 `disabled` 且不发请求；`approval` 需宿主传真实 `authorize` 回调，拒绝时不发请求。这是 SDK 策略，服务端当前没有旧 invocation 审批协调器。

### 原生 Runner 包装

Python 传 `agents.Runner` 类或实例，TypeScript 传 `new Runner()` 或 Runner 类；两者均返回独立的 `TaskWorkflowRunner`，默认 `mode="disable"`。`auto` 使用 `rsi.task` 和稳定的 `turn_id` / `turnId` 创建任务及执行请求，向克隆 Agent 注入冻结请求，再调用原生 Runner **一次**。原生结果直接返回，输出通过 `result_adapter` / `resultAdapter` 转成文本工件；默认把 `final_output` / `finalOutput` 保存为 `result.txt`，若结果本身包含 `files` 则逐个上传。默认从原生结果的 `new_items` / `newItems` 记录至多 200 条实际产生的条目，每条原始内容最多 4096 字节；有条目时声明 `trace_coverage=partial`，没有可读取条目时声明 `unknown`，不宣称完整工具轨迹。宿主可提供 `trace_adapter` / `traceAdapter` 从原生结果提取更完整的实际观察事件和覆盖声明；原子层验证事件 ID、数量、payload 和覆盖枚举。需要独立的业务事件来源时也可使用手动 atoms 提交。

```python
from agents import Runner
from cosens import cosens

runner = cosens(Runner, mode="auto", api_key=key, state_path="var/workflow.sqlite3")
native_result = await runner.run(agent, user_input, rsi={
    "turn_id": stable_turn_id,
    "task": {"source": "new_task", "objective": objective, "artifacts": input_artifact_ids},
})
turn = runner.cosens.get_turn(stable_turn_id)
# turn 中保存 task_id、execution_id、revision_id；后续动作由用户决定。
```

```typescript
import { Runner } from '@openai/agents';
import { cosens } from 'cosens-sdk';

const runner = cosens(new Runner(), { mode: 'auto', apiKey: key, statePath: './workflow.sqlite3' });
const nativeResult = await runner.run(agent, userInput, { rsi: {
  turnId: stableTurnId,
  task: { source: 'new_task', objective, artifacts: inputArtifactIds },
} });
const turn = runner.cosens.getTurn(stableTurnId);
```

Python 提供 `run/run_sync/run_streamed`，其中 `run_streamed` 和原生 Runner 一样同步返回流对象（准备阶段使用同步 HTTP）；TypeScript 提供 `run/runStreamed`。原生审批中断保留原生状态，可调用 `pending_state` / `resume`（TypeScript 为 `pendingState` / `resume`）。若结果已生成但提交失败，返回原生结果并保存 `completion_pending`；用 `reconcile(turn_id)` 复用上传回执和完全相同的提交正文。若 Artifact 上传回执未知，停止并人工对账。已进入 `native_running`、`native_failed`、`incomplete` 或等待原生审批时不自动重放 Agent。同 Session 的并发顺序由宿主管理。

Python `run_sync` 使用原生 `Runner.run_sync` 和同步 HTTP 原子客户端；同步失回执使用 `reconcile_sync(turn_id)`。两种语言均将稳定 `turn_id` 与原生输入摘要、Session ID 和 Session incarnation 绑定，准备阶段重试不能换输入或会话；恢复和原生审批续接也要传入原 Session。相同 Session 中不同 turn 的执行顺序仍由宿主管理。

原生结果缺少输出时，不会上传伪造的 `null` 产物；状态停在 `completion_pending`，宿主需提供真实结果材料后人工处理。事件通知回调失败单独记在 turn 状态中，不改变已确认的执行结果。

## 7. 验收矩阵与未完成目标

已实现范围：两种语言的手动 HTTP atoms、显式 start、独立 Runner 包装；Python 同步 atoms；现有双请求头、安全相对路径、HTTP 错误转换。仓库内 mock 测试覆盖方法路径、正文与键、Runner 原生结果与失回执恢复。IDEA 配置 `00_all_tests_CN` 可运行 Python/TypeScript 全量离线测试、两种语言的 CN 只读检查与新工作流冒烟测试；`00_all_tests_CN_live` 额外运行两种语言各三个真实模型示例。新冒烟脚本在工作流路由可用时创建唯一标记的合成任务，保存 Task/Execution/Artifact ID 和冻结的结果正文，并验证结果提交的同内容重试。2026-10-28 实测 Python 229 项、TypeScript 139 项本地测试通过，六个旧协议真实模型示例均成功；CN 网关 `/api/openapi.json` 返回 200、版本 `1.0.0`，**新工作流路径数为 0**。因此新冒烟脚本只读退出，当前新代码尚不能做该网关的真实写入联调。运行记录位于本机 `python/docs/testing/latest-cn/`；US 缺少有效 Key。

2026-10-29 更新：固定 CN 区域后，两种语言的新 Task 主链已完成真实联调，包含冻结执行请求、实际本地计算、上传输出、相同正文重试和读回核对。Python 268 项、TypeScript 152 项离线测试及构建/类型检查通过；新增真实 Agents SDK 的非流式/流式工具审批与同 Execution 恢复测试。流式取消会保留 Python 原生取消参数；TypeScript 等待 `completed` 遇到原生异常会保留原异常并记录 `incomplete`。具体证据见 [CN v3 验证记录](testing/2026-10-29-v3-cn-contract.json)。上述 2026-10-28 的“路径不存在”属于历史状态。

后续独立目标仍包括完整流生命周期覆盖、跨阶段自动宿主执行和跨进程并发顺序；第 8 节一次性上传已按当前约定实现，真实网关验收待下游部署后进行。旧 `/v1/runs` 包装器不能标为新工作流接入。一个 turn 对应一个 Execution；`approval` 必须由宿主或网关真实执行授权，不能暗示服务端已有旧 invocation 审批器。

## 8. 2026-10-29 新版 HTTP 契约与待开发项

本节记录 `RSI_WORKFLOW_API(3).md` 相对上一版的增量及 2026-10-29 的 SDK 实现结果。**Task → Execution → Revision → Attribution → Acceptance → Recipe Candidate → Verification** 的主要路径和请求正文未发生变化；新版重点补充旧 RSI Run、Memory、文档上传与内部/管理 HTTP 路由，并声明旧 RSI/Memory MCP 和 stdio MCP 入口已移除。SDK 以 HTTP 契约设计这些能力，不再以 `tools/list` 或 MCP 工具名作为验收依据。

### 8.1 身份、部署与接口边界

原始服务要求每个请求携带 `X-User-ID`，缺失返回 401，超过 128 字符或含控制字符返回 400；健康检查、OpenAPI、内部及管理路由也不例外。该字段不承担认证。原始服务不验证 Bearer，也不验证管理员身份，因此外部网关必须完成 Key 认证、用户身份绑定、管理员授权和资源隔离。SDK 对公共网关继续发送 `X-Auth-Type: api_key` 与 `Authorization: Bearer <api_key>`；网关如何注入 `X-User-ID` 是**部署验收门槛**，不可在 SDK 中猜测或以用户可控字符串代替。新版附件自述对应未部署的服务端分支，不能据此宣称现有 CN/US 网关支持新路由。

`/v1/...` 是面向网关的公共业务接口；`/internal/v1/...`、`/memory/internal/v1/...` 及 `/internal/admin/v1/...` 属于内部或管理边界。现有 `admin.atoms` 已有少量管理方法，但下游服务自身不校验管理员身份，调用者必须依赖网关真实实施授权。不要因附件列出内部路径就把它们加入默认公共 `client.atoms` 或普通透明包装。附件索引列出了更多 HTTP 路由；静态 OpenAPI JSON 未提供，涉及通用 `dict` 的请求还须以实际网关 Schema、回执和服务端约束共同核对。

### 8.2 Memory 公共接口

| 方法与路径 | 新版服务契约 | 当前 Python / TypeScript SDK | 后续开发要求 |
| --- | --- | --- | --- |
| `POST /v1/memory/ingest` | `content`、正文内 `request_key` 必填；可选 `source_id`、`expected_revision`（默认 0）、`durable`（默认 false）、`source_kind`（默认 `message`）。响应含 `source_id`、`revision`、`job_id`、`content_hash`、`state`；`pending` 不等于抽取完成。同用户同键同正文返回原回执，变参返回 409。 | 两端已增加 `source_id` / `sourceId` 与 `expected_revision` / `expectedRevision`，省略时不发送。为兼容旧 SDK 调用，仍显式发送 `source_kind=decision`、`durable=true`；需要原始服务默认语义时传 `message`、`false`。 | CN 实测 Python 创建/更新来源、TypeScript 继续更新同一来源、同键重试和 409 冲突均通过；最新作业仍 pending。失回执沿用同键同正文，不创建重复来源。 |
| `GET /v1/memory/jobs/{job_id}` | 按当前用户查询 `pending/committed/failed`；只有 `committed` 表示抽取与召回准备完成。 | 已有 `get_memory_job` / `getMemoryJob`。 | 以状态而非 HTTP 200 判断完成，保留原 job ID 供恢复。 |
| `POST /v1/memory/recall` | `query`、`token_budget`；服务默认预算 1500、最大 8000；返回 `items`、`estimated_tokens`、`diagnostics`。 | 两端仍显式发送 SDK 默认 1000，已验证输入预算 1–8000。 | 两端 CN 独立召回通过；不要把 SDK 的 1000 误写成服务默认值。 |
| `GET /v1/memory/items?limit=...` | 列出当前用户来源与经验，limit 1–500，默认 200。 | 两端已新增专用原子方法，默认显式发送 200，并校验 1–500。 | 两端 CN 真实回执含 `sources`、`experiences` 列表和 `limit`；不能用兼容 Wiki 视图代替。 |
| `GET /v1/memory`；`GET /v1/memory/experiences/{id}?revision=...` | 前者是兼容 Wiki 视图，当前 `blocks` 为空；后者读取指定经验版本。 | 已有 `get_memory` / `getMemory` 和 `get_memory_experience` / `getMemoryExperience`。 | 不把空 `blocks` 解释为用户没有来源或经验；经验版本与来源有效性需按回执处理。 |

### 8.3 RSI Run 的 assist 与 guided 执行

新版附件保留旧 `/v1/runs` 协议，并说明 `POST /v1/runs/prepare` 以必填 `Idempotency-Key` 创建冻结 Run：`objective` 必填，可选 `task_family`、`task_lineage_id`、`input_artifact_ids`、`deliverable`、`acceptance`、`environment`；`execution_mode` 仅支持 `assist/guided`，`managed` 尚不可用。回执包含 `run_id`、`status`、`task_package`、`recipe`、`context`。Python `prepare(..., execution_mode="guided")` 与 TypeScript `prepare({ executionMode: 'guided' })` 会发送 guided，客户端拒绝 `managed`；省略时仍由服务默认 assist。初始通用 Recipe 仅为未验证示例，不能等同已验证经验。

| 模式与阶段 | HTTP 接口 | 当前 SDK | 待开发或验收要点 |
| --- | --- | --- | --- |
| assist 建议与记录 | `POST /v1/runs/{id}/guidance`；`POST /v1/runs/{id}/checkpoint` | 两种语言已有 `guidance`、`checkpoint` 原子方法。 | guidance 的 `data` 可含 `observation`、`candidates`、可选 `node_id`/请求键；checkpoint 应记录真实 `decision_id`、`actual_action_id`、`next_state`。 |
| guided 取下一步 | `POST /v1/runs/{id}/steps/next` | Python `next_step`、TypeScript `nextStep` 已实现，CN 已联调。 | 请求无正文；`step_ready` 回执含 `node`、`attempt_id`、`attempt_token`、`attempt`、`repair_errors`、`max_repairs`。`node.output_contract` 声明端口，保存完整 attempt 身份。 |
| guided 提交步骤 | `POST /v1/runs/{id}/steps/{attempt_id}/submit` | Python `submit_step`、TypeScript `submitStep` 已实现，CN 已联调。 | 平铺提交原 `attempt_token`、真实 `outputs`；可附 `decision_id`、`actual_action_id`。artifact 端口传 `[{artifact_id,name,content_hash}]`，使用服务上传回执；单独 ID 会返回 `repair_required`，成功为 `step_passed`。回执未知时先查状态，不能重做可能有副作用的宿主步骤。 |
| guided 完成 | `POST /v1/runs/{id}/complete` | Python `complete_guided`、TypeScript `completeGuided` 已实现。 | 全部步骤完成后调用；与 assist 的既有 `/submit` 分开，不假设两个终结接口等价。 |
| 读取 Run 事件 | `GET /v1/runs/{id}/events` | Python `get_events`、TypeScript `getEvents` 已实现，CN 已联调。 | `/events` 与 `/trace` 不可混为一谈；CN OpenAPI 没有分页参数，真实回执为含 `items` 列表的对象。 |
| 反馈与经验 | `POST /v1/runs/{id}/feedback`；`GET /v1/runs/{id}/decisions/{decision_id}` | 两种语言已有 `feedback`、`get_decision`。 | `feedback` 不等于经验 `verified`；实际动作经 outbox 写入 Memory，初始经验可能为 `pending`。可信评价由管理接口处理，不能由普通反馈伪造。 |

### 8.4 文档上传

用户确认下游尚未完成部署，并要求先开发。Python 异步/同步和 TypeScript 已提供以下原子方法，每次只发一个 HTTP 请求：

| Python `atoms` / `sync_atoms` | TypeScript `atoms` | 网关 `/api` 后路径与请求 |
| --- | --- | --- |
| `create_document_upload_grant(filename=..., mime_type=..., **metadata)` | `createDocumentUploadGrant(metadata)` | `POST /v1/document/upload-grants`，JSON 元数据 |
| `upload_document_to_grant(grant, content=...)` | `uploadDocumentToGrant(grant, content)` | `PUT /v1/document/uploads/{upload_token}`，原始二进制，`Content-Type: application/octet-stream` |
| `upload_document(filename=..., mime_type=..., content=..., **metadata)` | `uploadDocument({ filename, mimeType, content, ...metadata })` | `POST /v1/document/upload`，multipart 的 `file` 部分携带文件名、MIME 类型和字节，其余字段为表单元数据 |
| `get_document_status(document_ids)` | `getDocumentStatus(documentIds)` | `POST /v1/document/status`，JSON `{"document_ids": ["..."]}`，必须是非空 ID 列表 |

契约依据：grant 路径来自 `RSI_WORKFLOW_API(3).md`；字段参考本机下游 Memory 源码 `backend/app/document_upload_grants.py`、`backend/app/core/schemas.py`；multipart 与 status 路径另由 CN OpenAPI 核对。当前 grant 约定为顶层 `upload_url`、`method: "PUT"`，可带 `ok`、`status: "awaiting_upload"`、`expires_at`、`max_bytes`。这属于部署前 SDK 适配约定，尚未获得该 HTTP 路由的真实回执；不能据此宣称服务端已经上线。下游部署后需逐项比对实际 Schema、身份转发和成功回执。

元数据必填 `filename`、`mime_type`，均为非空文本，不接受换行或 NUL。可选 `thread_id`、`uploaded_at`、`source_kind`、`workspace_id`、`ingest_mode`、`summary`、`background`、`wait_for_processing`（布尔）、`wait_timeout_seconds`（整数 1–3600）。TypeScript 参数对应 `mimeType`、`threadId`、`uploadedAt`、`sourceKind`、`workspaceId`、`ingestMode`、`waitForProcessing`、`waitTimeoutSeconds`；其余同名。可选字段省略时不发送。Python 内容接受非空 `bytes`、`bytearray` 或 `memoryview`；TypeScript 接受非空 `Uint8Array`（包括 Node `Buffer`）。SDK 不自动读取文件或转成 Base64。普通 multipart 与文本工件 `/v1/artifacts` 语义不同。

签发和上传均发送 `X-Auth-Type: api_key`、`Authorization: Bearer <api_key>`，由网关将同一 Key 映射为同一用户；SDK 不接受客户端指定 `user_id`、`graph_id` 或 `X-User-ID`。一次性上传只接受当前区域网关同源 HTTPS URL，路径必须为 `/api/v1/document/uploads/<token>`，或服务相对路径 `/v1/document/uploads/<token>`、`/api/v1/document/uploads/<token>`；token 只含字母、数字、下划线和连字符。拒绝跨区域/外部主机、不同端口、URL 用户信息、查询/片段、路径穿越、编码路径及重定向。不会执行回执中的 `curl`、`next_step`，也不转发回执自带的任意请求头。

上传前要求 `method=PUT`，若有 `ok=false` 或非 `awaiting_upload` 状态则拒绝；若有 `expires_at`，须为带时区的未来时间；若有 `max_bytes`，须为正整数且足以容纳实际内容。服务端仍是令牌过期和一次性消费的最终裁决者。传输异常和 HTTP 错误会脱敏 Key 与 upload token；成功 JSON 回执原样返回，调用者应妥善保存，避免记录令牌。HTTP 200 可能包含 `ok=false` / `status=failed`，SDK 不把它改写为处理成功。

所有方法不自动重试、重新签发或轮询。宿主持久化 grant 和上传回执；上传结果不明时不要再次消费令牌或新建上传。已知 `document_id` 时可查询状态；未知 ID 时需由下游核对。SDK 的一次性上传原子方法不提供跨进程恢复状态机。`wait_for_processing` 仅是服务端参数；客户端默认请求超时仍为 30 秒，长时间处理宜提交后按 ID 查询。

截至 2026-10-29，上传代码和离线测试已经完成；CN OpenAPI 尚无 grant 路径，探测为 404，一次性上传端到端验收待部署。本地测试不依赖该接口上线，已纳入 IDEA 总测试入口。

### 8.5 其他公共路由的范围

新版附件还附有完整 HTTP 路由索引，但部分路径只是补充列出旧服务能力，不能仅凭这次文档差异判断它们是新上线接口。为避免后续开发时遗漏或误认为已支持，先按资源族登记：

| 路由族 | 当前 SDK 状态与边界 |
| --- | --- |
| `GET /v1/runs`、`GET /v1/cases` 及 `/v1/cases/{id}/...` | 当前无专用列表/Case 原子方法；需从实际 OpenAPI 确认筛选、分页及 Case 产物/Recipe 权限，再决定是否扩展公共只读 SDK。 |
| `GET /v1/recipes/{task_family}` | 当前已有 `GET /v1/recipes/active` 的包装，但没有按 task family 读取 Recipe 的独立原子方法；两者语义不可互换。 |
| `/v1/memory/write`、`/search`、`/context`、episode/community 查询删除 | 当前只包装本节 8.2 的 ingest/recall/job/experience/兼容 Wiki；这些 Memory 原始能力尚无公共 SDK 原子方法，需单独界定范围和删除操作授权。 |
| `/v1/graph/*`、`/v1/context/assemble`、`/v1/content/chunk`、`/v1/document/ingestions`、`/v1/documents/{id}/download` | 当前无 CoSens 专用原子方法；附件未给出完整请求/响应细节，不能由路由名推造类型。`POST /v1/graph/write` 在附件索引中标为 retired，不应作为新开发目标。 |
| `/v1/improvements/{id}/attribution/...`、`/v1/trigger/decide`、`/v1/user/...`、各健康路由 | 属旧改进、触发、用户和运维路径；不得与新 Task 的 Attribution 或 SDK 本地 `auto` 决策混同。若纳入公共 SDK，须另核对网关可见性和权限。 |

### 8.6 内部与管理接口的处理范围

新版附件补充 `/internal/v1/experiences/{id}/outcomes`、`/withdraw`、`/internal/v1/experiences/search`、`/internal/v1/evidence/expand`、`/internal/v1/recipe-experiences/{observe,learning-view,recall}` 等 Memory 内部接口，以及 `/internal/admin/v1/memory-outbox` 的读取/重试、审批查询/批准、Recipe bootstrap、评价和改进发布接口。它们用于可信评价、来源/经验校验及运维，不属于普通 Task Runner 自动调用链。运行时经验搜索默认考虑 `production/train` 中当前 verified 记录；显式 `partitions` 当前仅允许 `['production']`，`screen/confirm/held_out` 不参与运行时搜索。

当前 SDK 仅暴露已有的少量 `admin.atoms`（例如 bootstrap bundle、决策/Recipe 评价及改进发布），没有新版附件的大部分内部/管理方法。本轮先记录接口分层，不推导管理员身份、不开放内部通用 HTTP 客户端；若后续确需 SDK 管理能力，必须先确定网关授权、可访问路由和最小方法集。新版附件给出的服务端数据库、provider、模型运行器和迁移配置属于部署资料，不是客户端可设置参数。

### 8.7 后续开发与验收顺序

1. 从目标网关取得新版本 OpenAPI 和回执，确认 `/api` 前缀、新工作流与 Run/Memory 路由是否实际部署；同时验证双认证头、网关注入的稳定 `X-User-ID`、管理路由授权。附件中的服务端分支与 2026-10-28 的 CN 网关 `1.0.0` 状态不能互相替代。
2. 在 Python 异步/同步 atoms 和 TypeScript atoms 对齐公共 Memory 新参数、`/v1/memory/items` 及 guided Run 的 `steps/next`、步骤提交、`complete`、`/events`；逐项核对状态、请求键、错误和未知副作用恢复。旧 Task 工作流接口保持独立。
3. 已按用户要求提前完成第 8.4 节文档上传、二进制/multipart 传输和本地防重试约束；待下游部署后核对 grant Schema、同区域目标、身份映射及处理回执。内部/管理接口另行限定权限与用途。
4. 两端新增方法已有离线契约测试。2026-10-29 IDEA 同一总入口 `python/examples/run_all_cn_tests.py` 的两端全量检查、CN 只读和 Task → Execution → Revision 冒烟测试通过（含相同正文重试、冻结请求哈希和输出工件读回）；加入真实 Agents SDK 审批/恢复及文档上传测试后，总入口复测通过，离线结果为 Python 268 项、TypeScript 152 项及构建/类型检查通过。两端 guided 完整步骤流均取得 `step_passed`、`completed` 及事件回执；Memory 来源更新、稳定键重试和旧版本冲突均已验证，但最新抽取作业仍 pending。CN `/api/openapi.json`（版本 `1.0.0`，167 条路径）没有 `/v1/document/upload-grants`，该路径的 OPTIONS 也返回 404，一次性上传适配已按第 8.4 节约定完成离线实现，真实端到端验收待部署。此前手动检查的 401 由 `.env` 默认 `COSENS_REGION=us` 导致，不能归为 CN 凭证失败；固定 CN 后相同 Key 的只读业务请求均为 200。US 仍无有效 Key，不属于本轮 CN 验收。
