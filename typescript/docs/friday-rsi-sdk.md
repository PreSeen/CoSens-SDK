# Friday 2.0 接入 CoSens RSI SDK 接口文档

| 项目 | 说明 |
| --- | --- |
| 契约状态 | 待实现的 TypeScript 接口。 |
| 目标包版本 | `cosens-sdk@0.5.4-rsi.25`，计划预发布。 |
| 当前对照版本 | `RSI_SDK_v2` 的 `d687648`，包版本 `0.5.4-rsi.24`。 |
| 导入路径 | `cosens-sdk/rsi`，运行环境为 Node.js 22+。 |

Friday Server 在用户确认交付后创建学习任务，保存 SDK 返回的 Execution ID 和 Recipe ID。Desktop 从 SDK 查询问题并逐题回答。SDK 完成本地测试和发布后，Friday Server 读取本次版本并交接云端产物。

## 1. 初始化和模型配置

```ts
type RSIConfig = {
  model: string;
  base_url: string;
  api_key: string;
  headers?: Record<string, string>;
  credential_scope: string;
  auto_start: boolean;
  context_window_tokens?: number;
  max_context_bytes?: number;
};

// RSIClient 构造参数
type RSIClientOptions = { data_dir: string; config: RSIConfig | null };
```

| 参数 | 约定 |
| --- | --- |
| `data_dir` | 本地 SQLite、证据快照和 Worker 检查点目录。Friday 为每个已认证账号分配独立目录。 |
| `model`、`base_url` | 模型 ID 和 API 基地址，例如 `https://gateway.example.com/v1`。SDK 发起 `POST /chat/completions`，使用支持工具调用的 OpenAI-compatible Chat Completions 协议。 |
| `api_key`、`headers` | `api_key` 作为 Bearer 凭证。SDK 合并附加请求头，保留 SDK 设置的 `Authorization` 和 JSON `Content-Type`，头名按大小写不敏感处理。 |
| `credential_scope` | Friday 根据已认证账号、模型提供方和凭证槽生成的稳定标识。凭证轮换沿用此标识。恢复时还需匹配初次执行绑定的 `model` 和 `base_url`。 |
| `auto_start` | `true` 受理后自动调度 Worker。用 `false` 时由宿主调用 `recover()`。 |
| `context_window_tokens` | 可选的模型窗口配置。省略时 SDK 使用保守的上下文预算。 |
| `max_context_bytes` | 可选的单份原生 trace 字节配额，独立于普通附件和 Recipe 包的配额。 |

`config: null` 用于读取记录。每次入队绑定创建请求对应的模型设置。宿主通过进程间管道向本次 Execution 的 Worker 传递凭证。SQLite 保存模型、端点和作用域，凭证和自定义头值只保存在进程内存。模型凭证随 Client 传入。

```ts
import { RSIClient } from "cosens-sdk/rsi";

const gatewayConfig = {
  model: gatewayModel,
  base_url: gatewayBaseUrl,
  api_key: accessToken,
  headers: { "X-Friday-Auth-Type": "access_token" },
  credential_scope: gatewayCredentialScope,
  auto_start: true,
};
const client = new RSIClient({ data_dir: accountDataDir, config: gatewayConfig });

const customClient = new RSIClient({
  data_dir: accountDataDir,
  config: {
    model: customModel,
    base_url: customBaseUrl,
    api_key: customApiKey,
    headers: customHeaders,
    credential_scope: customCredentialScope,
    auto_start: true,
  },
});
```

## 2. 创建 Execution

```ts
type ExecutionEvidence = {
  context_path: string;
  files: Array<{
    path: string;
    role: "input" | "artifact" | "reference" | "feedback";
  }>;
};

type ExecutionCreate = {
  evidence: ExecutionEvidence;
  recipe_reference_paths: string[];
  idempotency_key: string;
};

type ExecutionCreateReceipt = Execution & { recipe_id: string };
// RSIClient.execution_create(request: ExecutionCreate): Promise<ExecutionCreateReceipt>
```

- `context_path` 指向截至材料截止回复的完整原生 trace 文件。SDK 保留原始文件快照，将分析分段进行。普通文件通过 `files` 提交，空集合用 `[]`。
- `input` 同时用于学习和自动重跑。确认产物 `artifact`、参考 `reference`、反馈 `feedback` 和原生 trace 供学习阶段使用。测试在独立工作区，根据新生成的 Recipe 重跑本次任务。
- `recipe_reference_paths` 为必填数组，填写本次任务实际使用过的 Recipe 版本的 `RECIPE.md` 路径，供 RSI 学习参考。可传多个路径，没有时传 `[]`。每条路径指向一个独立版本目录的入口。SDK 在受理时快照该版本包，保留其中脚本、模板及相对路径。遇到缺失的包、无法读取的资源或越出包根目录的资源引用，返回输入错误。
- 每个新请求在受理事务中分配 SDK Recipe ID 和 Execution ID。Recipe 尚无版本时即可按 ID 查询。重复提交相同请求和 `idempotency_key` 返回同一对 ID，并复用已受理的快照。相同键提交不同材料或模型绑定返回 `conflict`。凭证轮换可沿用原键。

```ts
const run = await client.execution_create({
  evidence: {
    context_path: nativeTracePath,
    files: [
      { path: inputPath, role: "input" },
      { path: artifactPath, role: "artifact" },
    ],
  },
  recipe_reference_paths: [recipeReferencePath],
  idempotency_key: requestId,
});

const executionId = run.id;
const sdkRecipeId = run.recipe_id;
```

`execution_create()` 返回一个 Execution 对象，`id` 即 Execution ID，`recipe_id` 一定是字符串。首次受理时 `recipe_version_id` 为 `null`。Friday 用这一对 ID 关联自己的任务记录。

## 3. 查询 Recipe 和 Execution

| 方法 | 参数与返回 |
| --- | --- |
| `recipe_list(query?)` | `query` 可传 `{ recipe_ids: string[] }`。返回 `RecipeListEntry[]`，每个 Recipe 一行。 |
| `recipe_get(recipe_id, version)` | `version` 可传 `"active"`、`"latest"` 或具体版本 ID。返回 Recipe 及 `selected_version`，没有匹配选择器的版本时为 `null`。指定具体版本 ID 时，版本须属于该 Recipe，否则返回 `not_found`。 |
| `execution_get(execution_id)` | 返回指定 Execution 的完整公开详情。 |
| `execution_list(query?)` | `query` 可传 `{ execution_ids: string[] }`。直接返回 `Execution[]`，字段与 `execution_get()` 一致。 |

`recipe_get()` 顶层返回 `id`、`name`、`created_at`、`updated_at`、`archived_at`、`latest_version_id`、`active_version_id`、`latest_qualified_version_id` 和 `selected_version`。`revision` 和 `description` 已从 Recipe 顶层移除。

两个列表均按 ID 去重，跳过未匹配 ID，空数组返回 `[]`。省略 `query` 时，`recipe_list()` 返回完整展示列表，`execution_list()` 返回全部未归档执行。按明确 ID 查询 Execution 时也会返回匹配的已归档执行。Execution 列表按创建时间从旧到新排列。

| Recipe 列表字段 | 类型 | 含义 |
| --- | --- | --- |
| `recipe_id` | `string \| null` | SDK Recipe ID。历史未绑定的展示行可为 `null`。 |
| `execution_ids` | `string[]` | 所有关联的未归档 Execution ID，待处理执行优先。 |
| `name` | `string` | 展示名称。 |
| `status` | `RecipeListStatus` | 展示状态，例如 `queued`、`needs_clarification`、`needs_credentials`、`testing`、`passed`。 |
| `available_version_id` | `string \| null` | 当前可用版本 ID。 |
| `created_at`、`updated_at` | `number` | Unix 秒时间戳。 |

Recipe 展示状态可取 `active`、`inactive`、`queued`、`analyzing`、`generating`、`testing`、`needs_clarification`、`needs_credentials`、`passed`、`test_failed`、`unverifiable`、`completed`、`failed`、`cancelled`。

| Execution 字段 | 含义 |
| --- | --- |
| `id`、`name`、`created_at`、`updated_at` | ID、展示名称、创建和更新时间。时间为 Unix 秒。 |
| `status` | `queued`、`running`、`needs_clarification`、`needs_credentials`、`completed`、`failed`、`cancelled`。 |
| `stage`、`progress_percent`、`input_revision` | 当前阶段、0–100 的整数进度、输入版本。 |
| `recipe_id`、`recipe_version_id`、`rubrics_version_id` | 关联 SDK Recipe、候选版本和 Rubrics 版本。候选和 Rubrics 产生前各自为 `null`。历史未绑定记录的 `recipe_id` 可为 `null`。 |
| `base_execution_id` | 历史续接记录的父 ID。按本契约新建及原 ID 续跑时为 `null`。 |
| `evidence[]` | 文件快照元数据，每项包含 `path`、`role`、`locator`、`sha256` 和 `size`（字节）。 |
| `questions[]`、`answers` | 当前问题列表，以及按 `问题 ID:问题版本` 索引的已提交答案。 |
| `test_results[]` | 测试记录及各评分项。可用 `input_revision` 区分各轮输入。 |
| `result` | 本轮结果。未完成时为 `null`。完成后包含 `test_status`（`passed`、`failed` 或 `unverifiable`）、`provisional` 和 `published`。 |
| `error` | 错误详情包含 `code`、`type`、`stage`，有上游状态时还包含 `upstream_status`。无错误时为 `null`。 |

```ts
const [recipe] = client.recipe_list({ recipe_ids: [sdkRecipeId] });
const executions = client.execution_list({
  execution_ids: recipe?.execution_ids ?? [],
});
const pendingQuestions = executions
  .filter((run) => run.status === "needs_clarification")
  .flatMap((run) =>
    run.questions
      .filter((question) => !Object.hasOwn(run.answers, `${question.id}:${question.revision}`))
      .map((question) => ({ execution_id: run.id, question })),
  );
```

Desktop 用 `execution_get(execution_id)` 读取单条详情。问题含 `id`、`revision`、`question`、`assumption` 和 `options: { id, label }[]`。答案项包含 `question`、`question_revision`、`selected_option_ids`、`selected_options` 和 `text`。Friday 可直接使用这两项渲染和回显。

## 4. 提交澄清并在原 ID 续跑

```ts
type ClarificationAnswer = {
  question_id: string;
  question_revision: number;
  answer: string | string[];
};

type ClarificationReceipt = {
  execution_id: string;
  remaining_questions: number;
  resumed: boolean;
};
```

分析需要澄清时，Execution 进入 `needs_clarification`，Worker 释放调度资源。一次调用答一道题。选择题提交非空的选项 ID 数组，自由回答提交非空文本。

| 情况 | SDK 行为 |
| --- | --- |
| 仍有其他题待回答 | 保存答案，回执返回剩余题数及 `resumed: false`。 |
| 首次受理最后一题 | 在同一事务内保存答案、递增 `input_revision` 并将原 Execution 入队。回执为 `remaining_questions: 0`、`resumed: true`。Server 继续查询同一个 Execution ID。 |
| 重试已受理的相同答案 | 返回原回执。选项数组按集合比较，重试仅确认原处理结果。 |
| 对已受理的问题版本提交不同答案，或提交未受理的过期版本 | 返回 `conflict`。客户端可刷新问题。 |

`resumed` 表示本次受理触发了重新排队。若再次遇到必要澄清，SDK 在同一 Execution 下返回新问题版本。

```ts
const receipt = await client.execution_clarification_answer(executionId, {
  question_id: question.id,
  question_revision: question.revision,
  answer: [selectedOptionId],
});
```

## 5. 重启和凭证更新

`recover(query?)` 接受 `{ execution_ids: string[] }`，返回 `Promise<{ started: number; scheduled?: true }>`。省略筛选时，恢复范围为当前账号目录中与该 Client 的凭证作用域、模型和端点匹配的可恢复任务。传空数组时不调度任务。

Worker 可从原执行检查点接着处理。在重启后缺少凭证或上游拒绝凭证（例如 HTTP 401）时，执行进入 `needs_credentials`。Friday 取得同账号同凭证槽的新凭证后创建 Client，再恢复原 ID。显式传入的恢复任务与当前凭证绑定不匹配时返回 `credential_scope_mismatch`。旧 Worker 租约未到期时可返回 `scheduled: true`，到期后自动接管。

```ts
const refreshedClient = new RSIClient({
  data_dir: accountDataDir,
  config: { ...gatewayConfig, api_key: refreshedAccessToken },
});
await refreshedClient.recover({ execution_ids: [executionId] });
```

澄清等待与凭证等待是两个阶段。问题尚未答完时，恢复调用只调度其他符合条件的任务。最后一题触发入队后，如果当前 Client 缺少匹配凭证，等 Friday 补充凭证再执行。

升级前先停止旧版 Worker。历史记录和已生成版本仍可按原 ID 查询。迁移在途任务的账号凭证绑定需要原宿主确认；恢复接口按已保存的绑定进行校验。

## 6. 长 trace

Server 传完整原生文件。SDK 以流式方式保存快照并计算哈希。超出配置的原生 trace 配额时，在受理阶段返回 `invalid_argument`，宿主可调整配额后重试。

Worker 内部工具 `read_evidence({ locator, cursor? })` 返回本段文本、证据定位符及 `next_cursor`。首段省略游标，末段的游标为 `null`。游标绑定同一快照，分段保证 UTF-8 字符完整，有记录分隔符时优先按记录边界读取。SDK 根据模型上下文预算依次处理到文件末尾，再合并带原文定位的学习结果。分页、分析和汇总都由 SDK 处理。

## 7. 完成回执与云端交接

Friday Server 定时用本次 Execution ID 查询。交接条件为 `status === "completed"`、`result.test_status === "passed"`、`result.published === true`、没有待回答问题，并且本次 Execution 写出了 `recipe_version_id`。用该 ID 精确获取版本。版本的 `content` 为 `{ title, applicability, instructions }`。

```ts
const current = client.execution_get(executionId);
const hasPendingQuestions = current.questions.some(
  (question) => !Object.hasOwn(current.answers, `${question.id}:${question.revision}`),
);

if (
  current.status === "completed" &&
  current.result?.test_status === "passed" &&
  current.result.published === true &&
  !hasPendingQuestions &&
  current.recipe_id === sdkRecipeId &&
  current.recipe_version_id
) {
  const { selected_version: version } = client.recipe_get(
    sdkRecipeId,
    current.recipe_version_id,
  );
  if (version?.id !== current.recipe_version_id) {
    throw new Error("SDK Recipe version unavailable");
  }
  const completion = {
    execution_id: current.id,
    recipe_id: sdkRecipeId,
    recipe_version_id: version.id,
    content: version.content,
  };
  // Friday 使用 completion 组装 RECIPE.md，并处理云端产物。
}
```

Friday 根据 `recipe_authoring_tasks.target_recipe_id` 处理云端产物。该字段为空时新建并安装，有值时更新目标 Recipe 并等待用户确认应用。本流程按精确版本读取产物，列表里的 `available_version_id` 用于展示和选择当前可用版本。

## 8. 其他本地接口

| 方法 | 用途 |
| --- | --- |
| `execution_append_evidence(execution_id, evidence)` | 为进行中的执行补充材料，返回更新后的 Execution。 |
| `execution_cancel(execution_id)` | 取消未终结的执行，返回 Execution。 |
| `recipe_test({ execution_id, recipe_id, version_id, rubrics_version_id })` | 安排指定版本的测试。Friday authoring 流程使用创建后自动安排的测试。 |
| `recipe_version_set_archived(recipe_id, { version_id, archived })` | 归档版本，或恢复最新通过验证的版本。 |
| `rubrics_list({ offset, archived })` | 分页查询评分标准，每页 50 条。 |
| `rubrics_get(rubrics_id, version)` | 读取指定评分标准版本。可传 `"latest"` 或版本 ID。 |
| `close()` | 释放 Client 的 SQLite 连接。 |
