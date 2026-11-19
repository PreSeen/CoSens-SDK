# CoSens 本地 RSI SDK 接口文档

适用于 `RSI_SDK_v2` 本地实现。TS 从 `cosens-sdk/rsi` 导入，Python 从 `cosens.rsi` 导入。本地 RSI 接口只接收必填参数。`null`（Python 为 `None`）用于明确表示首次学习。

```text
Agent 启动 → recover()
任务开始 → recipe_list() → Agent 选择可用版本 → recipe_get(recipe_id, version_id)
交付确认 → execution_create（上下文和文件）
查看进度 → recipe_list() → execution_get(id)
测试通过且澄清完成 → SDK 自动发布版本 → 旧版本自动归档
```

## 初始化

- TS：`new RSIClient({ data_dir, config })`
- Python：`RSIClient(data_dir=data_dir, config=config)`

`data_dir` 是本地数据目录。`config` 为 `{ model, base_url, auto_start }`，创建 Execution 时提供，查询和恢复已有工作时可以传 `null`。三项均必填。`base_url` 是模型 API 基地址，支持 Chat Completions 和工具调用。`auto_start=true` 在入队后自动派发 Worker，`false` 时由宿主调用 `recover()`。模型密钥从 `RSI_MODEL_API_KEY` 读取。TS 如需覆盖启动 Worker 的 Node 路径，设置 `RSI_NODE_EXECUTABLE`。

## 业务接口

TS 的创建和测试参数放在一个对象内，Python 的字段用关键字传入。读写已有对象时，两版先传对象 ID。分页列表接收分页对象。TS 直接把 `evidence` 对象传给追加接口，并把澄清和版本归档的字段放在 ID 后的参数对象内。Python 使用关键字。`recipe_get` 和 `rubrics_get` 的第二项在 TS 中是位置参数，在 Python 中使用关键字。

| 对象 | 方法 | 必填参数 | 返回与用途 |
| --- | --- | --- | --- |
| Execution | `execution_create` | `evidence`, `base_execution_id`, `idempotency_key` | 排队创建 Execution。 |
| Execution | `execution_get` | `execution_id` | 读取名称、阶段、进度、问题、测试结果和错误。 |
| Execution | `execution_list` | `offset`, `archived` | 分页列出 Execution 历史及各自进度。 |
| Execution | `execution_append_evidence` | `execution_id`, `evidence` | 给进行中的 Execution 追加证据并重新分析。 |
| Execution | `execution_cancel` | `execution_id` | 取消排队或进行中的 Execution。 |
| Execution | `execution_clarification_answer` | `execution_id`, `question_id`, `question_revision`, `answer` | 提交澄清。返回 Execution ID 及可空的子 Execution ID。 |
| Rubrics | `rubrics_list` | `offset`, `archived` | 分页列出各套评分标准及最新版本 ID。 |
| Rubrics | `rubrics_get` | `rubrics_id`, `version` | `version` 传 `"latest"` 或版本 ID，返回选中的版本和评分项。 |
| Recipe | `recipe_list` | 无 | 返回首次学习、可用版本和可以恢复的归档条目。 |
| Recipe | `recipe_get` | `recipe_id`, `version` | `version` 传 `"active"`、`"latest"` 或版本 ID，返回选中版本。 |
| Recipe | `recipe_test` | `execution_id`, `recipe_id`, `version_id`, `rubrics_version_id` | 在活动 Execution 内安排指定版本测试。 |
| Recipe | `recipe_version_set_archived` | `recipe_id`, `version_id`, `archived` | `true` 归档指定版本，`false` 恢复最新通过验证的版本。返回 `recipe_id`、`version_id`、`archived`。 |

`recipe_list()` 中的 `available_version_id` 指向当前可用版本。Agent 将 `recipe_id` 和 `available_version_id` 传给 `recipe_get`。手动归档当前版本时，条目保留，`archived=true`、`available_version_id=null`，可用 `latest_qualified_version_id` 恢复。首次学习或验证未通过时，`available_version_id` 也可能为空，此时用 `archived` 区分。

在没有未决澄清时，上一个可用版本保留到编号更高的版本验证通过。切换和旧版归档在同一个事务完成。旧版可按 ID 读取，`selected_version.archived` 表示所查版本的归档状态。恢复仅支持最新通过验证的版本。手动归档只作用于指定版本，更新的版本通过验证后仍会自动可用。

对象分页固定每页 50 条。`offset` 从 0 开始，`archived=false` 查未归档对象，`true` 查已归档对象。

### Execution 返回结构

`execution_create`、`execution_get`、`execution_list` 的条目以及追加证据、取消接口使用相同的 Execution 字段。

| 字段 | 含义 |
| --- | --- |
| `id`, `name`, `created_at`, `updated_at` | ID、显示名称和 Unix 秒时间戳。 |
| `base_execution_id` | 续接的历史执行 ID，首次学习为 `null`。 |
| `status`, `stage`, `progress_percent`, `input_revision` | 执行状态、阶段、进度和输入版本。状态为 `queued`、`running`、`completed`、`failed` 或 `cancelled`。 |
| `evidence[]` | 每条包含 `path`、`role`、`locator`、`sha256` 和 `size`（字节）。 |
| `questions[]`, `answers` | 问题和按 `问题ID:问题版本` 键控的答案。迟到答案触发子执行时，对应答案带 `continuation_execution_id`。 |
| `recipe_id`, `recipe_version_id`, `rubrics_version_id` | 关联的 Recipe、候选版本和实际采用的评分标准版本，相应阶段完成前为 `null`。 |
| `test_results[]` | 所有测试轮次的记录，可按 `input_revision` 选择本轮评分。 |
| `result` | 流程结束后包含 `test_status`、`provisional` 和 `published`。`published` 表示完成时的发布结果，当前可用版本以 `recipe_list()` 为准。未完成时为 `null`。 |
| `error` | 失败信息，含 `code`、`type`、`stage`，有上游状态时含 `upstream_status`。无错误时为 `null`。 |

新版首次打开数据目录时，会在同一事务中将旧业务记录迁到 `executions`、`recipes`，删除废弃字段和通用的 `objects` 表。升级前先停止旧版本 Worker。升级后使用同一版本的 SDK 读取和写入该数据目录。Agent 从 Execution 顶层读取三个关联 ID，并用 `result.published` 展示完成时的发布结果。

### Execution 进度

`execution_get` 和 `execution_list` 返回整数 `progress_percent`（0–100）。排队为 0%，分析为 10%。默认两轮设置下，首轮生成、测试和评分为 25%、40%、55%，第二轮为 65%、75%、90%。进度表示工作阶段，最终结果保存后为 100%。测试结果见 `result.test_status`。

执行失败或取消时保留已记录的进度。Worker 恢复同一输入版本时，进度保持单调。追加证据、接受澄清或安排重测使 `input_revision` 递增时，新一轮进度随该输入版本重新计算。界面可在存在待回答的澄清问题时优先显示「待澄清」。

### Execution 证据和测试

- `evidence` 由必填的 `context_path` 和 `files` 组成。无附加文件时传 `[]`。每个文件都需要 `path` 和 `role`，角色为 `input`、`artifact`、`reference` 或 `feedback`。
- SDK 在创建 Execution 时复制文件快照。详情的 `evidence[]` 返回文件列表，包含角色、哈希和定位符。旧记录沿用原快照读取。
- 宿主在取得用户交付确认后调用接口。SDK 使用 `idempotency_key` 记录本次提交。
- SDK 自动用 Recipe 重跑当前任务。创建时标为 `input` 的文件同时用于学习和测试。测试在独立工作区生成新产物，再按 Rubric 评分。上下文、历史参考（`reference`）、确认产物（`artifact`）和反馈只用于学习。该得分衡量当前任务能否复现。
- 首次学习或独立学习时，`base_execution_id` 传 `null`（Python 为 `None`）。要续接时，填已结束的 Execution ID。SDK 创建一条新 Execution，锁定原执行当时生成的 Recipe 版本作为基线，并使用本次提交的 `evidence` 学习和测试。原执行没有生成版本时只记录续接关系。原执行仍在进行时，使用 `execution_append_evidence` 补充材料。
- `idempotency_key` 是本次提交的稳定非空字符串。同一操作重试保留原键和输入。旧接口已经提交的执行可按 ID 查询或恢复，使用旧提交的幂等键提交新格式会报冲突。
- Worker 自动提炼并锁定 Recipe 名称、生成 Rubrics、保存候选及测试结果。自动学习最多两轮。

### 更新和澄清参数

- `execution_append_evidence` 使用与创建相同的 `evidence` 结构，只提交新增内容。新证据递增输入版本并触发重新分析，测试继续使用创建时的输入范围。重试相同证据批次会复用原快照。
- `question_id` 和 `question_revision` 来自 `execution_get().questions`。 `answer` 为选中的选项 ID 数组或非空文本。已完成执行如果返回 `continuation_execution_id`，宿主应跟踪该子执行。

## 本地 SQLite 存储

| 表 | 内容 |
| --- | --- |
| `executions` | **业务主表。** 记录状态、阶段、进度、续接关系及关联版本。`created_at` 用于排队，`owner`、`lease_until`、`fence` 用于 Worker 认领、续租和接管。澄清、评分和错误等明细存于 `details`。 |
| `recipes` | **业务主表。** Recipe 名称、当前可用版本、最新候选和最新通过验证的版本使用独立列。 |
| `versions` | Recipe 和 Rubrics 的版本内容。通过 `recipe_id` 或 `rubrics_id` 归组。`rubrics_metadata` 保存评分标准名称、时间等元数据，Rubrics 列表按组读取最新版本。 |
| `evidence_snapshots` | 保存 Execution 的上下文与文件快照元数据。 |
| `slots` | 协调同一数据目录内的 Worker 和模型调用并发。 |
| `idempotency` | 保存幂等请求及其响应，用于安全重试。 |

`recipe_list` 查询 `recipes`、`executions` 和 `versions`，首次学习时从 `executions` 创建展示条目。初始化会在同一事务内升级旧库到 SQLite schema v3，保留已有记录 ID、版本和 Worker 租约。升级前先停止使用同一数据库的旧版 Worker。

## 运行管理

| 方法 | 参数 | 用途 |
| --- | --- | --- |
| `recover` | 无 | Agent 启动时调用一次。返回 `started`，旧租约尚未过期时可返回 `scheduled=true`。 |
| `close`（TS） | 无 | 释放宿主持有的 SQLite 连接。 |

TS 需等待 `execution_create`、`execution_append_evidence`、`execution_clarification_answer`、`recipe_test` 和 `recover` 返回。Python 接口按同步方式调用。

## Memory 服务接口

宿主 Agent 直接调用 Memory 原子函数。调用时读取环境变量 `COSENS_API_KEY`，通过 US 算法网关 `https://rsi.cosens.ai/api` 发送请求，认证头为 `Authorization: Bearer <COSENS_API_KEY>` 和 `X-Auth-Type: api_key`。

| TS 方法 | Python 方法 | 参数（TS / Python） | 服务接口 |
| --- | --- | --- | --- |
| `memoryRecall` | `memory_recall` | `query`，`tokenBudget` / `token_budget` 默认 1000，范围 1–8000 | `POST /v1/memory/recall` |
| `memoryIngest` | `memory_ingest` | 必填 `content`、`requestKey` / `request_key`。可选 `sourceKind` / `source_kind`（默认 `decision`）、`durable`（默认 `true`）、`sourceId` / `source_id`、`expectedRevision` / `expected_revision` | `POST /v1/memory/ingest` |
| `getMemoryItems` | `get_memory_items` | `limit` 默认 200，范围 1–500 | `GET /v1/memory/items` |
| `getMemoryJob` | `get_memory_job` | 任务 ID | `GET /v1/memory/jobs/{job_id}` |
| `getMemoryExperience` | `get_memory_experience` | 经验 ID，可选 `revision` | `GET /v1/memory/experiences/{experience_id}` |
| `getMemory` | `get_memory` | 无 | `GET /v1/memory` |

`memoryRecall` / `memory_recall` 返回服务响应，其中 `items` 为召回条目。写入重试使用相同的 `requestKey` / `request_key`。HTTP 错误通过 `MemoryError` 暴露状态码，以及服务返回的错误码和请求 ID。

```ts
import { memoryRecall } from 'cosens-sdk/memory';

const { items } = await memoryRecall({ query: taskIntent });
```

```python
from cosens.memory import memory_recall

recalled = await memory_recall(query=task_intent)
items = recalled["items"]
```
