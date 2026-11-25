# CoSens RSI SDK 接口文档

云端模式默认启用，本地模式使用 SQLite schema v10。`recipe_get()` 返回 Recipe 文件的 `storage_path`。TS 从 `cosens-sdk/rsi` 导入，Python 从 `cosens.rsi` 导入。

```text
Agent 启动 → recover()
任务开始 → recipe_list() → Agent 选择可用版本 → recipe_get(recipe_id, version_id)
交付确认 → execution_create（上下文和文件）
查看任务卡片 → task_list() → task_get(task_id)
测试通过且澄清完成 → SDK 激活本地版本并打包 ZIP → App 按 task_id 调用 recipes_upsert
```

## 初始化

### 云端模式（默认）

TS 可用 `new RSIClient()` 或 `new RSIClient({ mode: "cloud", api_key, base_url })`。Python 可用 `RSIClient()` 或 `RSIClient(mode="cloud", api_key=..., base_url=...)`。

| 参数 | 必填条件 | 类型、默认值和用途 |
| --- | --- | --- |
| `mode` | TS 传云端配置对象时必填。Python 选填。 | 取值 `"cloud"`。无参构造默认选择云端。 |
| `api _key` | 选填。有可用凭证才能发起调用。 | 非空字符串，省略时读取 `COSENS_API_KEY`。 |
| `base_url` | 选填。 | 字符串，省略时依次使用 `COSENS_RSI_BASE_URL` 和 `https://rsi.cosens.ai/api`。 |

TS 也可以直接 `new CloudClient({ api_key, base_url })`，配置对象和两个字段均可省略。该类提供 `request(method, path, options?)`，其中 HTTP 方法 `method` 和相对 API 路径 `path` 必填，`options.body` 和 `options.params` 选填。TS 云端请求超时为 60 秒。Python 直接构造 `CloudRSIClient(api_key=..., base_url=..., timeout=..., transport=...)` 时，四项均选填，`timeout` 默认 60 秒，`transport` 默认 `None`，可传自定义 HTTPX transport。这两个参数属于 `CloudRSIClient`。

学习由 CoSens 服务端执行。SDK 上传 `evidence` 中列出的文件和选填的 `recipe_reference` 文件，单次编码后 JSON 请求体上限为 10 MiB。

### 本地模式

TS 用 `new RSIClient({ data_dir, model_id, api_key, base_url, config })`。Python 用 `RSIClient(data_dir=..., model_id=..., api_key=..., base_url=..., config=...)`，可显式传 `mode="local"`。只读查询可只传 `data_dir`，也可同时传 `config: null`（TS）或 `config=None`（Python）。只上传已有任务时，初始化可只传 `data_dir` 和包含 `headers` 的 `config`。

| 参数 | 必填条件 | 类型、默认值和用途 |
| --- | --- | --- |
| `data_dir` | 必填。 | TS 为路径字符串，Python 可传字符串或 `Path`。它是 SQLite、文件快照和 Recipe 文件的保存根目录。 |
| `model_id` | 运行本地 Execution 时必填。 | 非空字符串，传入 Chat Completions 请求体的 `model` 字段。 |
| `api_key` | 运行本地 Execution 时必填。 | 模型 API Key，SDK 将它作为 Bearer token 发送。 |
| `base_url` | 运行本地 Execution 时必填。 | 支持工具调用的 OpenAI-compatible Chat Completions 基地址。 |
| `config` | 选填。 | 本地运行和 Recipe 云端读写选项。 |
| `config.headers` | 选填，省略等同 `{}`。 | 本地 SDK 查找、下载和上传云端 Recipe 时使用的 HTTP 请求头，键和值都是字符串。 |
| `config.credential_scope` | 选填。需要在 token 刷新后自动匹配同账号任务时传入。 | 宿主为已认证账号指定的稳定非空标识。同账号刷新 token 时沿用该值，共用 `data_dir` 的不同账号使用不同值。SQLite 保存标识的哈希。 |
| `config.auto_start` | 选填，默认 `true`。 | 为 `true` 时入队后自动调度 Worker，为 `false` 时由宿主调用 `recover()`。 |
| `config.max_tokens` | 选填。 | 正整数，限制单次模型调用的输出。省略时由模型服务决定。 |
| `config.timeout` | 选填。 | 秒数，默认 120，有效范围为大于 0 且不超过 3600。 |
| `config.test_python_image` | 选填。 | 本机已安装且由宿主信任的 Docker Python 镜像，例如 `python:3.12-slim`。设置后本地 Worker 可以在隔离容器中运行测试脚本。需要的依赖由镜像预装。 |

SDK 向 `<base_url>/chat/completions` 发送模型请求，使用 `Authorization: Bearer <api_key>`、`Content-Type: application/json` 和 SDK 的 `User-Agent`。模型 Key 只在 Client 和 Worker 进程使用，SQLite 只保存匹配标识。

更换模型 Key 后，用原 `data_dir` 和 `config.credential_scope`、新的 `api_key` 创建 Client，再调用 `recover()`。Worker 接手时使用本次 Client 的模型、地址、凭证、超时和输出预算。历史上依赖 `RSI_MODEL_API_KEY` 的执行仍可通过原环境变量恢复。TS 可通过 `RSI_NODE_EXECUTABLE` 指定 Worker 的 Node 可执行文件。

## 业务接口

TS 的创建和测试参数放在一个对象内，Python 的字段用关键字传入。读写已有对象时，两版先传对象 ID。分页列表接收分页对象。TS 直接把 `evidence` 对象传给追加接口，并把澄清和版本归档的字段放在 ID 后的参数对象内。Python 使用关键字。`recipe_get` 和 `rubrics_get` 的第二项在 TS 中是位置参数，在 Python 中使用关键字。

| 方法 | 必填参数 | 选填参数 | 返回与用途 |
| --- | --- | --- | --- |
| `execution_create` | `evidence`（对象）、`idempotency_key`（非空字符串） | `recipe_reference`（对象），参数见下表。 | 排队创建 Execution，返回执行的 `id` 和关联的 `recipe_id`。 |
| `execution_get` | `execution_id`（字符串） | 无 | 读取名称、阶段、进度、问题、测试结果和错误。 |
| `execution_list` | 无 | `query`，提供时须含 `execution_ids: string[]`（Python 为 `list[str]`）。 | 返回 Execution 列表。 |
| `task_list` | 无 | 无 | 本地模式。每个任务返回一条卡片摘要，包含当前执行的状态、进度和待答问题数量。 |
| `task_get` | `task_id`（首次 Execution ID） | 无 | 本地模式。返回任务 ID、Recipe 和版本 ID、最终 ZIP 路径、当前执行及历次澄清问答。 |
| `task_restart` | `task_id`、`idempotency_key` | `evidence`（新材料）。 | 本地模式。创建后续 Execution，从分析开始，保留原执行的历史。 |
| `execution_append_evidence` | `execution_id`（字符串）、`evidence`（对象） | 无顶层选填项。`evidence` 字段要求见下表。 | 给进行中的 Execution 追加证据并重新分析。 |
| `execution_cancel` | `execution_id`（字符串） | 无 | 取消排队或进行中的 Execution。 |
| `execution_clarification_answer` | `execution_id`、`question_id`（字符串）、`question_revision`（正整数）、`answer`（非空文本或非空选项 ID 数组） | 无 | 提交单题答案，返回原执行 ID 和可空的子执行 ID。 |
| `rubrics_list` | `offset`（非负整数）、`archived`（布尔值） | 无 | 分页列出评分标准及最新版本 ID。 |
| `rubrics_get` | `rubrics_id`（字符串）、`version`（`"latest"` 或版本 ID） | 无 | 返回选中的评分标准版本。 |
| `recipe_list` | 无 | `query`，提供时须含 `recipe_ids: string[]`（Python 为 `list[str]`）。 | 返回首次学习条目和现有 Recipe 的摘要。 |
| `recipes_upsert` | 本地模式传 `task_id`。云端模式传 ZIP 路径或 `file://` URI。 | `recipe_id`（指定已有云端 Recipe）和 `idempotency_key`。 | 上传 ZIP 并返回正式的 `recipe_id`、`recipe_version_id`。本地模式同步更新 SQLite 中的 ID。 |
| `recipe_download` | `recipe_id`、`recipe_version_id`（TS 第二个位置参数对应版本 ID） | 无 | 云端模式读取指定版本的 ZIP，Python 返回 `bytes`，TS 返回 `Uint8Array`。 |
| `recipe_get` | `recipe_id`（字符串） | `version`（`"active"`、`"latest"` 或版本 ID，默认 `"active"`） | 返回 Recipe 和 `selected_version`。 |
| `recipe_test` | `execution_id`、`recipe_id`、`version_id`、`rubrics_version_id`（均为字符串） | 无 | 在活动 Execution 内安排指定版本测试。 |
| `recipe_version_set_archived` | `recipe_id`、`version_id`（字符串）、`archived`（布尔值） | 无 | `true` 归档版本，`false` 恢复最近发布的版本。返回两个 ID 和归档状态。 |

### `execution_create()` 的材料参数

| 参数 | 必填条件 | 类型和用途 |
| --- | --- | --- |
| `idempotency_key` | 必填。 | 非空字符串。同一请求重试用同一个键。 |
| `evidence.context_path` | 必填。 | 上下文文件路径。 |
| `evidence.files` | 本地模式和 TS 云端模式必填。Python 云端模式可省略，默认 `[]`。 | 文件项数组，无附加文件时传 `[]`。本地模式与参考 Recipe 合计最多 1000 个文件。 |
| `evidence.files[].path` | 每个文件项必填。 | 本地文件路径。 |
| `evidence.files[].role` | 每个文件项必填。 | 取值 `input`、`artifact`、`reference` 或 `feedback`。 |
| `recipe_reference` | 选填，Python 可传 `None`。 | 对象，指定一份供首轮生成参考的 Recipe。 |
| `recipe_reference.path` | 提供参考对象时必填。 | 参考 Recipe 的本地文件路径，支持直接使用已有版本的 `RECIPE.md`。 |
| `recipe_reference.recipe_id` | 选填。 | 字符串。传入时关联已有 Recipe，新版本归到该 ID。省略或在 Python 中传 `None` 时，使用参考文件学习并新建 Recipe。 |
| `recipe_reference.recipe_version_id` | 本地模式选填。使用时须同时提供 `recipe_id`。 | 指定更新的基准版本。 |

`recipe_reference` 在创建时提供。普通本地参考文件的正文用于首轮生成，本地 Execution 的 `evidence` 用 `role="recipe"` 标识对应的快照。普通历史材料使用 `role="reference"`。

在新设备上更新云端 Recipe 时，传入正式 `recipe_id`。如果本地缺少基准版本，SDK 会从 CoSens 查询当前已发布且可用的版本，校验 ZIP 并保存包内全部文件，然后用包内的 `RECIPE.md` 创建任务。提供 `recipe_version_id` 时锁定该版本。若版本缺少本地记录并且云端已切到其他可用版本，SDK 返回版本冲突。下载使用 `COSENS_RSI_BASE_URL` 或默认的 CoSens 地址，并通过 `config.headers` 或 `COSENS_API_KEY` 认证。本地模型 Key 留在模型调用链路。

SQLite 采用包的正式 Recipe ID 和版本 ID，并保存云端关联。缓存就绪后可以离线使用该版本。生成的新版本保留基准版本的资源，测试时可以使用这些资源。新产物仍通过 `recipes_upsert()` 将新版本写入同一个云端 Recipe。

```ts
const run = await client.execution_create({
  idempotency_key: 'delivery-42',
  evidence: {
    context_path: contextPath,
    files: [],
  },
  recipe_reference: { path: previousRecipeFile, recipe_id: recipeId },
});
```

```python
run = client.execution_create(
    idempotency_key="delivery-42",
    evidence={
        "context_path": context_path,
        "files": [],
    },
    recipe_reference={"path": previous_recipe_file, "recipe_id": recipe_id},
)
```

`execution_append_evidence()` 的 `evidence` 使用相同的 `context_path`、`files`、`path` 和 `role` 规则，角色限于 `input`、`artifact`、`reference`、`feedback`。

本地完整文本上下文随 `context_path` 创建快照，无固定单文件或批次大小上限。Agent 通过 `read_evidence(locator, offset?, limit?, query?)` 读取，每页最多 8192 字符。按 `next_offset` 继续读取，或用 `query` 从指定位置查找下一处精确匹配。在较长的工具调用历史中，旧页的正文会从发给模型的请求中移除，原始快照和读取轨迹可供追查。普通附件以及云端上传仍适用各自的大小限制。

### Recipe 文件格式

Recipe Markdown 使用 UTF-8，开头的 YAML 包含非空字符串 `name` 和 `description`，由两行 `---` 包围。模型生成的正文必须是非空 Markdown。`selected_version.content` 将正文放在 `instructions` 字段。示例文件如下。

```markdown
---
name: "order-export"
description: "按用户要求将订单数据导出为 CSV。"
---

## 步骤

按指定列顺序生成 CSV。
```

### 任务卡片和重新开始

首次 `execution_create()` 返回的 `id` 是稳定的 `task_id`。`task_list()` 按任务返回当前的 `execution_id`、状态、阶段、进度、`test_status` 和待答问题数。用 `task_get(task_id)` 读取 `execution_ids`、`pending_questions` 和 `clarification_history`。历史执行状态可凭各自的 ID 通过 `execution_get()` 查看。续跑时历史问题及已提交的答案仍可查看，只有当前有效的问题可以回答。提交答案仍调用 `execution_clarification_answer()`，使用当前执行 ID、问题 ID 和修订号。

主动重新开始时，TS 调用 `task_restart(taskId, { idempotency_key, evidence })`，Python 调用 `task_restart(task_id, idempotency_key=..., evidence=...)`。可以省略新证据。SDK 创建下一条 Execution，继承材料和已提交的答案，从分析开始。重试同一次重启请求时沿用幂等键。最终产物已经确认的任务保留历史结果，后续更新 Recipe 通过 `execution_create()` 创建新任务。

`task_get()` 顶层返回 `task_id`、`recipe_id`、`recipe_version_id` 和 `recipe_zip_path`。任务关联 Recipe 后即可读到 `recipe_id`。最终版本通过验证、澄清完成并生成 ZIP 后，`recipe_version_id` 与绝对路径 `recipe_zip_path` 才有值，此前二者均为 `null`。产物就绪时，`artifact` 也包含同一对 ID 和 ZIP 路径。上传成功后用云端正式 ID。历史任务的本地 ZIP 已清理时，仍可通过回执读取正式 ID，路径为 `null`，可用云端 `recipe_download()` 获取该版本。

### 发布 Recipe ZIP

在本地模式下，传入要发布的 `task_id`。SDK 查找该任务通过验证的最终版本和冻结 ZIP，再上传。可先用 `task_get(task_id)` 读取 `recipe_zip_path` 判断产物是否就绪。

传入正式的 `recipe_id` 时，SDK 将 ZIP 发布为该云端 Recipe 的新版本，回执返回同一个 `recipe_id` 和新签发的 `recipe_version_id`。省略时，SDK 沿用任务已关联的云端 Recipe，或在首次发布时创建 Recipe。任务已关联其他云端 Recipe，或指定 ID 已属于另一份本地 Recipe 时，SDK 返回冲突。

```python
receipt = client.recipes_upsert(task_id=task_id, recipe_id=cloud_recipe_id)
```

```ts
const receipt = await client.recipes_upsert({ task_id: taskId, recipe_id: cloudRecipeId });
```

本地 `recipes_upsert()` 从环境变量 `COSENS_RSI_BASE_URL` 读取上传地址，省略环境变量时使用默认 CoSens 地址。发起上传请求时，认证和账号信息取自初始化 `RSIClient` 时传入的 `config.headers`，其中可包含 `X-Virtual-User-Id`。SDK 生成 multipart 请求头，使用传入的 `idempotency_key` 或自动生成幂等键。

上传成功后，SDK 按回执更新任务行的 `recipe_id` 和 `recipe_version_id`，并同步更新关联的 Execution 引用。`recipes.id` 始终是任务 ID。后续用 `execution_create()` 更新同一 Recipe 时可传正式 `recipe_id`。

网络中断后用同一个 `task_id` 重试。SDK 使用之前保存的 ZIP 和幂等键。为同一个 Recipe 上传另一版本时，先完成当前待确认的上传。若同一 Recipe 的 Execution 正在排队或运行，等该执行结束后再同步。

在云端模式下，直接把 ZIP 路径传给 `recipes_upsert()`。传入已有的正式 `recipe_id` 时发布新版本，省略时创建 Recipe。

### Recipe 详情返回结构

本地 `recipe_get()` 返回 `id`、`name`、`created_at`、`updated_at`、`archived_at`、`storage_path`、`latest_version_id`、`active_version_id`、`latest_qualified_version_id`、`selected_version`。已同步的 Recipe 和版本在 `id` 和 `selected_version.id` 返回云端正式 ID。

`storage_path` 在入队时分配，值为相对于 `RSIClient.data_dir` 的固定目录。SDK 保存候选版本时创建目录和 Markdown 文件。用 `selected_version.id` 拼出具体版本文件 `<data_dir>/<storage_path>/<version_id>/RECIPE.md`。同步后目录仍由 `storage_path` 指定，原版本文件路径继续可读。SQLite 保存版本正文和状态，Markdown 文件供宿主读取和复用。

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
| `execution_ids` | 字符串数组 | 关联的未归档 Execution ID。 |
| `name` | 字符串 | 条目显示名称。 |
| `active_version_id` | 字符串或 `null` | 当前可用版本，可和 `recipe_id` 一起传给 `recipe_get`。 |
| `created_at` | 数字 | 创建时间，Unix 秒。已绑定 Recipe 时取 Recipe 记录，未绑定的历史条目取 Execution 记录。 |
| `updated_at` | 数字 | 更新时间，Unix 秒，取同一条 Recipe 或 Execution 记录。 |

`recipe_get()` 返回 Recipe 对象，其 ID 为 `id`。列表条目通过 `recipe_id` 引用 Recipe。两处的可用版本都叫 `active_version_id`。

首次学习时，`execution_ids` 包含本次 Execution 的 ID。已有 Recipe 没有关联执行时返回 `[]`。未归档的 Execution 按创建时间从新到旧排序，创建时间相同时按 ID 倒序。

`active_version_id` 表示 Recipe 当前可用的版本。例如学习 v2 时等待澄清，v1 仍可用，则此字段仍指向 v1。任务进度和澄清状态通过 `task_list()` 或 `task_get()` 读取。手动归档可用版本后，条目仍在列表，可用版本 ID 变为 `null`。恢复时可用 `recipe_get(recipe_id, "latest")`（Python 用 `version="latest"`）读取 `latest_qualified_version_id`，再调用 `recipe_version_set_archived`。

在没有未决澄清时，上一个可用版本保留到编号更高的版本验证通过。切换和旧版归档在同一个事务完成。旧版可按 ID 读取，`selected_version.archived` 表示所查版本的归档状态。恢复仅支持最近发布的版本。手动归档只作用于指定版本，更新的版本通过验证后仍会自动可用。

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

- 本地 SDK 在创建 Execution 时复制文件快照。详情的 `evidence[]` 返回文件列表，包含角色、哈希和定位符。旧记录沿用原快照读取。
- 宿主在取得用户交付确认后调用接口。SDK 使用 `idempotency_key` 记录本次提交。
- SDK 自动用 Recipe 重跑当前任务。创建时标为 `input` 的文件同时用于学习和测试。测试在独立工作区生成新产物，再按 Rubric 评分。上下文、历史参考、确认产物、反馈和 `recipe` 文件只用于学习。该得分衡量当前任务能否复现。
- `idempotency_key` 是本次提交的稳定非空字符串。同一操作重试保留原键和输入。旧接口已经提交的执行可按 ID 查询或恢复，使用旧提交的幂等键提交新格式会报冲突。
- Worker 自动提炼并锁定 Recipe 名称、生成 Rubrics、保存候选及测试结果。自动学习最多两轮。
- 如果宿主显式调用 `recipe_test` 改测另一个 Recipe，Execution 会切换到指定的测试目标。原先为这一条执行预建、尚无任何版本的 Recipe 会归档。

### 测试中运行 Python

当 Rubric 要求运行 Python 且本地 Client 配置了 `test_python_image`，测试模型可用 `write_output` 把脚本写到 `scripts/`，再调用 `run_python({ path, args })`。`path` 是脚本相对路径，`args` 是参数数组，省略时为 `[]`。Docker 使用本机已有镜像。脚本在 `/workspace/outputs` 中运行，从 `/workspace/inputs` 读取本轮获准测试的只读文件。工具将真实退出码、标准输出和标准错误写入测试轨迹。测试产物由隔离工作区收集，待运行脚本与产物分开。

容器禁用网络并限制运行时间和资源。使用未声明 `VOLUME` 的可信镜像，由镜像预装任务所需的依赖，例如读取 Excel 所需的库。模型请求头、宿主的其他路径和学习材料不会挂载到容器。

分析识别到任务明确要求实跑 Python 时，Rubric 会标记 `requires_python: true`。评分继续使用原阈值。需要实跑而缺少成功执行证据时，Recipe 无法发布。镜像或 Docker 不可用时，测试结果为 `unverifiable`，详情见 `test_results[].reason` 和测试轨迹。评审根据实际执行和产物核对每项校验。

### 更新和澄清参数

- `execution_append_evidence` 接收新增的 `context_path` 和 `files`。新证据递增输入版本并触发重新分析，测试继续使用创建时的输入范围。重试相同证据批次会复用原快照。
- `question_id` 和 `question_revision` 来自 `execution_get().questions`。`answer` 为同一道题选中的选项 ID 数组或非空文本。多道题逐题提交到原 Execution。只要还有待回答的问题，SDK 就只保存答案，返回 `continuation_execution_id: null`。
- 最后一道题回答后，SDK 创建一个继承全部答案的子 Execution，并返回 `continuation_execution_id`。原执行尚在排队或运行时，会停止并标记为 `cancelled`。原执行已经完成时，保留已有测试结果。重试最后一道相同答案将复用同一个子 Execution。
- 宿主从返回的子 Execution ID 跟踪重新分析、生成和测试。子执行后续再次提出澄清问题时，按同样流程回答。
- 同一账号更换模型 Key 后，用相同的 `config.credential_scope`、原任务的 `model_id` 和 `base_url`、新的 `api_key` 创建 Client。最后一道答案通过归属校验后，SDK 用新的凭证自动启动同一 Recipe 的子 Execution。使用 `auto_start: true` 时，回答一次即可续跑。

## 本地 SQLite 存储

| 表 | 内容 |
| --- | --- |
| `executions` | **业务主表。** 记录状态、阶段、进度、续接关系及关联版本。`created_at` 用于排队，`owner`、`lease_until`、`fence` 用于 Worker 认领、续租和接管。澄清、评分和错误等明细存于 `details`。 |
| `recipes` | **每个任务一行。** `id` 是根 Execution 的 ID，也是任务主键。`recipe_id` 可由多个任务共享。任务最终通过验证后，写入 `recipe_version_id` 和相对 `data_dir` 的 `recipe_zip_path`。 |
| `rubrics_versions` | 保存评分标准的各版本和元数据。 |
| `evidence_snapshots` | 保存 Execution 的上下文与文件快照元数据。 |
| `slots` | 协调同一数据目录内的 Worker 和模型调用并发。 |
| `idempotency` | 保存幂等请求、上传回执、ID 别名，以及恢复候选版本和复用云端基准所需的 Recipe 数据。 |

TS 和 Python 使用相同的表结构。查询时，`latest_version_id` 取版本号最大的一条，`active_version_id` 取唯一可用版本，`latest_qualified_version_id` 取可用及已归档版本中编号最大的一条。`recipe_list()` 行里的 `active_version_id` 与 `recipe_get()` 的同名字段取值相同。

新版本验证通过并发布后，较早版本在同一事务中归档。手动归档当前可用版本后，可用版本 ID 为空。只允许恢复最近发布的版本。

初始化将旧库升级到 SQLite schema v10。迁移按根 Execution 补齐任务行，保留旧 ID 别名、上传回执和原产物路径。v8 已同步的记录从回执恢复正式 ID。v6 及更早的版本在原路径导出新格式的 `RECIPE.md`。升级前先停止使用同一数据库的旧版 Worker。

## 运行管理

| 方法 | 必填参数 | 选填参数 | 用途 |
| --- | --- | --- | --- |
| `recover` | 无。传 `retry_key` 时必填 `recipe_id`。 | `recipe_id`（SDK Recipe ID）、`previous_headers`（任务上次绑定的请求头）、`retry_key`（非空字符串，仅本地，与 `previous_headers` 互斥）。 | 返回 `started`。安排后台 Worker 等待执行时可返回 `scheduled: true`。带 `retry_key` 时还返回受理的 `recipe_id` 和 `execution_id`。云端由服务端自行恢复，默认调用返回 `{ started: 0 }`。 |
| `close` | 无 | 无 | TS 本地释放 SQLite 连接，TS 云端直接返回。Python 云端释放 HTTPX 连接。 |

TS 的 RSI 业务接口及 `recover()` 用 `await`，`close()` 同步调用。Python RSI 方法同步调用。

TS 用 `await client.recover({ recipe_id: recipeId })`，Python 用 `client.recover(recipe_id=recipe_id)`。指定 SDK Recipe ID 时只接手该 Recipe。省略 ID 时恢复同一账号可接手的全部 Execution，包括排队中的任务、租约到期的任务，以及更换凭证后可以重试的 HTTP 401 或 403 失败任务。继续使用原 Execution ID 和已有断点。没有匹配任务时返回 `{ started: 0 }`。

以前创建的任务如果没有 `credential_scope`，可用与创建时匹配的模型配置恢复。如果旧凭证与当前模型 Key 生成的 Bearer 请求头不同，首次续跑可在 `recover()` 传入 `previous_headers` 以匹配原绑定。模型调用使用当前 Client 的 `api_key`。若新 Client 也配置了 `credential_scope`，恢复成功后更换模型 Key 时可沿用该标识。

### 失败任务的显式重试

任务卡片上的「重试」调用 `recover`，同时传入 `recipe_id` 和 `retry_key`。SDK 找到该 Recipe 最近的 Execution，接受状态为 `failed`、错误码为 `timeout` 的模型调用超时。旧版本仍可用时，也可以重试这条最近失败的 Execution。普通恢复调用沿用上面的条件。

重试保留 Recipe ID、Execution ID、材料快照、已保存的候选版本和 LangGraph 断点。新 Client 使用原 `model_id` 和 `base_url`、同一 `config.credential_scope` 和当前有效的 `api_key`。旧任务没有 `credential_scope` 时，重试匹配原凭证绑定。调用 `recover` 会调度 Worker，即使 Client 设置了 `auto_start: false`。

```ts
const retry = await client.recover({ recipe_id: recipeId, retry_key: retryRequestId });
const execution = await client.execution_get(retry.execution_id);
```

```python
retry = client.recover(recipe_id=recipe_id, retry_key=retry_request_id)
execution = client.execution_get(retry["execution_id"])
```

每次重试使用一个稳定的 `retry_key`。响应丢失时沿用原键，SDK 返回同一对 ID，并在必要时补派发同一次重试。后台 Worker 已安排时返回 `scheduled: true`。宿主继续用原 Execution ID 查询进度和结果。重跑再次超时后，用户可用新键发起下一次重试。

## Memory 服务接口

宿主 Agent 直接调用 Memory 原子函数。调用时读取环境变量 `COSENS_API_KEY`，通过 US 算法网关 `https://rsi.cosens.ai/api` 发送请求，认证头为 `Authorization: Bearer <COSENS_API_KEY>` 和 `X-Auth-Type: api_key`。

| TS 方法 | Python 方法 | 必填参数（TS / Python） | 选填参数和默认值（TS / Python） | 服务接口 |
| --- | --- | --- | --- | --- |
| `memoryRecall` | `memory_recall` | `query`（字符串） | `tokenBudget` / `token_budget`（整数，默认 1000，范围 1–8000） | `POST /memory/recall` |
| `memoryIngest` | `memory_ingest` | `content`（字符串）、`requestKey` / `request_key`（1–256 字符） | `sourceKind` / `source_kind`（默认 `"decision"`）、`durable`（布尔值，默认 `true`）、`sourceId` / `source_id`（非空字符串）、`expectedRevision` / `expected_revision`（非负整数） | `POST /memory/ingest` |
| `getMemoryItems` | `get_memory_items` | 无 | `limit`（整数，默认 200，范围 1–500） | `GET /memory/items` |
| `getMemoryJob` | `get_memory_job` | `jobId` / `job_id`（任务 ID） | 无 | `GET /memory/jobs/{job_id}` |
| `getMemoryExperience` | `get_memory_experience` | `experienceId` / `experience_id`（经验 ID） | `revision`（版本号） | `GET /memory/experiences/{experience_id}` |
| `getMemory` | `get_memory` | 无 | 无 | `GET /memory` |

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
