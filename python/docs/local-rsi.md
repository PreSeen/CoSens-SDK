# CoSens RSI SDK 接口文档

云端模式默认启用，本地模式使用 SQLite schema v6。两种模式共用 RSI 方法，本地 `recipe_get()` 另返回 Recipe 文件的 `storage_path`。TS 从 `cosens-sdk/rsi` 导入，Python 从 `cosens.rsi` 导入。

```text
Agent 启动 → recover()
任务开始 → recipe_list() → Agent 选择可用版本 → recipe_get(recipe_id, version_id)
交付确认 → execution_create（上下文和文件）
查看进度 → recipe_list() → execution_get(id)
测试通过且澄清完成 → SDK 自动发布版本 → 旧版本自动归档
```

## 初始化

- 云端（默认）：TS `new RSIClient()`，Python `RSIClient()`。读取 `COSENS_API_KEY`，学习在 CoSens 服务端用 CoSens 的模型完成。只上传 `evidence` 里列出的文件。可传 `api_key`、`base_url`（TS 为 `{ mode: 'cloud', api_key, base_url }`）。云端的 `recover()` 直接返回 `{ started: 0 }`，服务端自行恢复。
- 本地：TS `new RSIClient({ data_dir, config })`，Python `RSIClient(data_dir=data_dir, config=config)`。TS 两种模式的方法都是 async。

本地模式下，`data_dir` 是 SQLite、证据快照和 Recipe 文件的保存根目录。创建 Execution 时，`config` 必须包含 `model`、`base_url` 和 `auto_start`。查询和恢复已有工作时可以传 `null`（Python 为 `None`）。可选 `max_tokens` 设置每次模型调用的输出上限，省略时由模型服务决定。`timeout` 设置单次请求秒数，默认 120。`base_url` 是支持 Chat Completions 和工具调用的模型 API 基地址。请求的 User-Agent 为 `cosens-sdk-python/<版本>`（TS 为 `cosens-sdk-typescript/<版本>`）。`auto_start=true` 在入队后自动派发 Worker，`false` 时由宿主调用 `recover()`。模型密钥从 `RSI_MODEL_API_KEY` 读取。TS 如需覆盖启动 Worker 的 Node 路径，设置 `RSI_NODE_EXECUTABLE`。

## 业务接口

TS 的创建和测试参数放在一个对象内，Python 的字段用关键字传入。读写已有对象时，两版先传对象 ID。分页列表接收分页对象。TS 直接把 `evidence` 对象传给追加接口，并把澄清和版本归档的字段放在 ID 后的参数对象内。Python 使用关键字。`recipe_get` 和 `rubrics_get` 的第二项在 TS 中是位置参数，在 Python 中使用关键字。

| 对象 | 方法 | 必填参数 | 返回与用途 |
| --- | --- | --- | --- |
| Execution | `execution_create` | `evidence`, `idempotency_key` | 排队创建 Execution，立即返回执行的 `id` 和关联的 `recipe_id`。 |
| Execution | `execution_get` | `execution_id` | 读取名称、阶段、进度、问题、测试结果和错误。 |
| Execution | `execution_list` | 无 | 列出未归档 Execution。可用 `query.execution_ids` 按 ID 筛选。 |
| Execution | `execution_append_evidence` | `execution_id`, `evidence` | 给进行中的 Execution 追加证据并重新分析。 |
| Execution | `execution_cancel` | `execution_id` | 取消排队或进行中的 Execution。 |
| Execution | `execution_clarification_answer` | `execution_id`, `question_id`, `question_revision`, `answer` | 提交澄清。返回 Execution ID 及可空的子 Execution ID。 |
| Rubrics | `rubrics_list` | `offset`, `archived` | 分页列出各套评分标准及最新版本 ID。 |
| Rubrics | `rubrics_get` | `rubrics_id`, `version` | `version` 传 `"latest"` 或版本 ID，返回选中的版本和评分项。 |
| Recipe | `recipe_list` | 无 | 返回首次学习条目和现有 Recipe 的摘要。可用 `query.recipe_ids` 按 ID 筛选。 |
| Recipe | `recipe_get` | `recipe_id`, `version` | `version` 传 `"active"`、`"latest"` 或版本 ID，返回 Recipe 基础字段、三个版本 ID 和 `selected_version`。 |
| Recipe | `recipe_test` | `execution_id`, `recipe_id`, `version_id`, `rubrics_version_id` | 在活动 Execution 内安排指定版本测试。 |
| Recipe | `recipe_version_set_archived` | `recipe_id`, `version_id`, `archived` | `true` 归档指定版本，`false` 恢复最新通过验证的版本。返回 `recipe_id`、`version_id`、`archived`。 |

### Recipe 详情返回结构

本地 `recipe_get()` 返回 `id`、`name`、`created_at`、`updated_at`、`archived_at`、`storage_path`、`latest_version_id`、`active_version_id`、`latest_qualified_version_id`、`selected_version`。

`storage_path` 在入队时分配，值为相对于 `RSIClient.data_dir` 的 `recipes/<recipe_id>`。SDK 保存候选版本时创建目录和 Markdown 文件。用 `selected_version.id` 拼出具体版本文件 `<data_dir>/<storage_path>/<version_id>/RECIPE.md`。归档和恢复沿用原路径。SQLite 保存版本正文和状态，Markdown 文件供宿主读取和复用。

```ts
import { join } from 'node:path';

const recipe = await client.recipe_get(recipeId, versionId);
const recipeFile = client.data_dir && recipe.selected_version
  ? join(client.data_dir, recipe.storage_path, recipe.selected_version.id, 'RECIPE.md')
  : null;
```

```python
recipe = client.recipe_get(recipe_id, version=version_id)
version = recipe["selected_version"]
recipe_file = (
    client.data_dir / recipe["storage_path"] / version["id"] / "RECIPE.md"
    if version
    else None
)
```

### Recipe 列表返回结构

`recipe_list()` 直接返回数组。省略 `query` 时返回完整列表。TS 用 `recipe_list({ recipe_ids: [id1, id2] })`，Python 用 `recipe_list(query={"recipe_ids": [id1, id2]})`。传入 `recipe_ids` 后，只返回 ID 匹配的 Recipe，保留列表原有顺序。重复 ID 只返回一行，未知 ID 会跳过，空数组返回 `[]`。

首次学习在入队时就有 Recipe ID。历史上还未绑定 Recipe 的执行可单独显示。每个 Recipe 只显示一行。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `recipe_id` | 字符串或 `null` | Recipe ID。尚未绑定 Recipe 的历史执行为 `null`。 |
| `execution_ids` | 字符串数组 | 关联的未归档 Execution ID。首项对应当前展示状态，可用 `execution_get` 查看进度、澄清问题和候选版本 ID。 |
| `name` | 字符串 | 条目显示名称。 |
| `status` | 字符串 | 当前展示状态，例如 `testing`、`needs_clarification` 或 `passed`。 |
| `available_version_id` | 字符串或 `null` | 当前可用版本，可和 `recipe_id` 一起传给 `recipe_get`。 |
| `created_at` | 数字 | 创建时间，Unix 秒。已绑定 Recipe 时取 Recipe 记录，未绑定的历史条目取 Execution 记录。 |
| `updated_at` | 数字 | 更新时间，Unix 秒，取同一条 Recipe 或 Execution 记录。 |

首次学习时，`execution_ids` 包含本次 Execution 的 ID。已有 Recipe 没有关联执行时返回 `[]`。数组优先排列待回答、排队、分析、生成和测试中的执行，再排列已结束的执行。每组按创建时间从新到旧排序。

状态取值为 `active`、`inactive`、`queued`、`analyzing`、`generating`、`testing`、`needs_clarification`、`passed`、`test_failed`、`unverifiable`、`completed`、`failed`、`cancelled`。

例如 v2 待回答而 v1 仍可用时，`status` 为 `needs_clarification`，`available_version_id` 仍指向 v1。手动归档后条目仍在列表，可用版本 ID 变为 `null`。恢复时可用 `recipe_get(recipe_id, "latest")`（Python 用 `version="latest"`）读取 `latest_qualified_version_id`，再调用 `recipe_version_set_archived`。

在没有未决澄清时，上一个可用版本保留到编号更高的版本验证通过。切换和旧版归档在同一个事务完成。旧版可按 ID 读取，`selected_version.archived` 表示所查版本的归档状态。恢复仅支持最新通过验证的版本。手动归档只作用于指定版本，更新的版本通过验证后仍会自动可用。

`rubrics_list()` 固定每页 50 条。`offset` 从 0 开始，`archived=false` 查未归档对象，`true` 查已归档对象。

### 按 ID 查询 Execution

TS 用 `execution_list({ execution_ids: [id1, id2] })`，Python 用 `execution_list(query={"execution_ids": [id1, id2]})`。省略 `query` 时返回全部未归档执行。按 ID 查询时也可读取匹配的已归档执行。结果按创建时间从旧到新排列，重复 ID 只返回一条，未知 ID 会跳过，空数组返回 `[]`。

### Execution 返回结构

`execution_create`、`execution_get`、`execution_list` 的条目以及追加证据、取消接口使用相同的 Execution 字段。

| 字段 | 含义 |
| --- | --- |
| `id`, `name`, `created_at`, `updated_at` | ID、显示名称和 Unix 秒时间戳。 |
| `base_execution_id` | 澄清等流程自动续接的父 Execution ID。通过 `execution_create` 直接创建时为 `null`。 |
| `status`, `stage`, `progress_percent`, `input_revision` | 执行状态、阶段、进度和输入版本。状态为 `queued`、`running`、`completed`、`failed` 或 `cancelled`。 |
| `evidence[]` | 每条包含 `path`、`role`、`locator`、`sha256` 和 `size`（字节）。 |
| `questions[]`, `answers` | 问题和按 `问题ID:问题版本` 键控的答案。最后一道答案触发子执行时，该答案带 `continuation_execution_id`。 |
| `recipe_id` | `execution_create` 返回的关联 Recipe ID。尚未绑定 Recipe 的历史执行可能为 `null`。 |
| `recipe_version_id`, `rubrics_version_id` | 候选版本和实际采用的评分标准版本，相应阶段完成前为 `null`。 |
| `test_results[]` | 所有测试轮次的记录，可按 `input_revision` 选择本轮评分。 |
| `result` | 流程结束后包含 `test_status`、`provisional` 和 `published`。`published` 表示完成时的发布结果，当前可用版本以 `recipe_list()` 为准。未完成时为 `null`。 |
| `error` | 失败信息，含 `code`、`type`、`stage`，有上游状态时含 `upstream_status`。无错误时为 `null`。 |

新版首次打开数据目录时，会在同一事务中将旧业务记录迁到 `executions`、`recipes`，删除废弃字段和通用的 `objects` 表。升级前先停止旧版本 Worker。升级后使用同一版本的 SDK 读取和写入该数据目录。Agent 从 Execution 顶层读取三个关联 ID，并用 `result.published` 展示完成时的发布结果。

### Execution 进度

`execution_get` 和 `execution_list` 返回整数 `progress_percent`（0–100）。排队为 0%，分析为 10%。默认两轮设置下，首轮生成、测试和评分为 25%、40%、55%，第二轮为 65%、75%、90%。进度表示工作阶段，最终结果保存后为 100%。测试结果见 `result.test_status`。

执行失败或取消时保留已记录的进度。Worker 恢复同一输入版本时，进度保持单调。追加证据或安排重测使 `input_revision` 递增时，新一轮进度随该输入版本重新计算。界面可在存在待回答的澄清问题时优先显示「待澄清」。

### Execution 证据和测试

- `evidence` 由必填的 `context_path` 和 `files` 组成。无附加文件时传 `[]`。每个文件都需要 `path` 和 `role`，角色为 `input`、`artifact`、`reference`、`feedback` 或 `recipe`。
- SDK 在创建 Execution 时复制文件快照。详情的 `evidence[]` 返回文件列表，包含角色、哈希和定位符。旧记录沿用原快照读取。
- 宿主在取得用户交付确认后调用接口。SDK 使用 `idempotency_key` 记录本次提交。
- SDK 自动用 Recipe 重跑当前任务。创建时标为 `input` 的文件同时用于学习和测试。测试在独立工作区生成新产物，再按 Rubric 评分。上下文、历史参考、确认产物、反馈和 `recipe` 文件只用于学习。该得分衡量当前任务能否复现。
- 创建时可传一份 `role: "recipe"` 的文件，例如 `{ "path": "/work/recipe.md", "role": "recipe", "recipe_id": "recipe_…" }`。首轮生成以该文件的快照内容为基线。填写 `recipe_id` 时，新版本归到该 Recipe 下，要求该 Recipe 存在且有可用版本。省略 `recipe_id` 时创建新 Recipe。两种情况均在入队时返回关联的 `recipe_id`。
- `idempotency_key` 是本次提交的稳定非空字符串。同一操作重试保留原键和输入。旧接口已经提交的执行可按 ID 查询或恢复，使用旧提交的幂等键提交新格式会报冲突。
- Worker 自动提炼并锁定 Recipe 名称、生成 Rubrics、保存候选及测试结果。自动学习最多两轮。
- 如果宿主显式调用 `recipe_test` 改测另一个 Recipe，Execution 会切换到指定的测试目标。原先为这一条执行预建、尚无任何版本的 Recipe 会归档。

### 更新和澄清参数

- `execution_append_evidence` 接收新增的 `context_path` 和 `files`。角色 `recipe` 仅在创建时提交。新证据递增输入版本并触发重新分析，测试继续使用创建时的输入范围。重试相同证据批次会复用原快照。
- `question_id` 和 `question_revision` 来自 `execution_get().questions`。`answer` 为同一道题选中的选项 ID 数组或非空文本。多道题逐题提交到原 Execution。只要还有待回答的问题，SDK 就只保存答案，返回 `continuation_execution_id: null`。
- 最后一道题回答后，SDK 创建一个继承全部答案的子 Execution，并返回 `continuation_execution_id`。原执行尚在排队或运行时，会停止并标记为 `cancelled`。原执行已经完成时，保留已有测试结果。重试最后一道相同答案将复用同一个子 Execution。
- 宿主从返回的子 Execution ID 跟踪重新分析、生成和测试。子执行后续再次提出澄清问题时，按同样流程回答。

## 本地 SQLite 存储

| 表 | 内容 |
| --- | --- |
| `executions` | **业务主表。** 记录状态、阶段、进度、续接关系及关联版本。`created_at` 用于排队，`owner`、`lease_until`、`fence` 用于 Worker 认领、续租和接管。澄清、评分和错误等明细存于 `details`。 |
| `recipes` | **业务主表。** 六列为 `id`、`name`、`created_at`、`updated_at`、`archived_at`、`storage_path`。`storage_path` 是相对于 `data_dir` 的固定目录。 |
| `versions` | Recipe 和 Rubrics 的版本内容。通过 `recipe_id` 或 `rubrics_id` 归组。Recipe 版本的 `status` 为 `candidate`、`available` 或 `archived`。`rubrics_metadata` 保存评分标准名称、时间等元数据。 |
| `evidence_snapshots` | 保存 Execution 的上下文与文件快照元数据。 |
| `slots` | 协调同一数据目录内的 Worker 和模型调用并发。 |
| `idempotency` | 保存幂等请求及其响应，用于安全重试。 |

TS 和 Python 使用相同的表结构。查询时，`latest_version_id` 取版本号最大的一条，`active_version_id` 取唯一可用版本，`latest_qualified_version_id` 取可用及已归档版本中编号最大的一条。`recipe_list()` 继续返回 `available_version_id`。

新版本验证通过并发布后，较早版本在同一事务中归档。手动归档当前可用版本后，可用版本 ID 为空。只允许恢复最新通过验证的版本。

初始化在一个事务内将旧库升级到 SQLite schema v6。v3 及更早的版本指针迁入版本状态，v4 的 `recipe_api_metadata` 在迁移中删除。SDK 为历史 Recipe 补齐 `storage_path`，并在同一 `data_dir` 下按版本导出已有正文。迁移保留 Recipe ID、版本内容与状态、执行记录和 Worker 租约。升级前先停止使用同一数据库的旧版 Worker。

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
