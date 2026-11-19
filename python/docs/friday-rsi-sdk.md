# Friday 2.0 接入 CoSens RSI SDK 接口文档（Python）

| 项目 | 说明 |
| --- | --- |
| 契约状态 | 待实现的 Python 接口，与 [TypeScript 接入契约](../../typescript/docs/friday-rsi-sdk.md) 对齐。 |
| 当前对照版本 | `RSI_SDK_v2` 的 `d687648`，Python 包版本为 `cosens-sdk==0.4.2`。 |
| 建议预发布版本 | `cosens-sdk==0.4.3rc1`。 |
| 导入路径 | `from cosens.rsi import RSIClient`，运行环境为 Python 3.10+。 |

Friday Server 在确认交付后创建学习任务，保存 SDK 返回的 Execution ID 和 Recipe ID。Desktop 从 SDK 查询问题并逐题回答。SDK 完成本地测试和发布后，Friday Server 读取本次版本并交接云端产物。Python 接口同步调用，学习和测试由后台 Worker 执行。

## 1. 初始化

`RSIClient(*, data_dir, config)` 接收本地数据目录和模型配置。仅查询记录时可传 `config=None`。Friday 为每个已认证账号分配独立数据目录。

| `config` 字段 | 类型 | 必填 | 用途 |
| --- | --- | --- | --- |
| `model` | `str` | 是 | 模型 ID。 |
| `base_url` | `str` | 是 | API 基地址，例如 `https://gateway.example.com/v1`。SDK 在其下调用 `/chat/completions`。 |
| `api_key` | `str` | 是 | Bearer 凭证。Gateway 传当前有效的 access token，自定义模型传其 API Key。 |
| `headers` | `dict[str, str]` | 否 | 附加请求头。SDK 保留自身设置的 `Authorization` 和 JSON `Content-Type`。头名按大小写不敏感处理。 |
| `credential_scope` | `str` | 是 | 由 Friday 根据已认证账号、模型提供方和凭证槽生成的稳定标识。凭证轮换时沿用。 |
| `auto_start` | `bool` | 是 | `True` 时受理后自动调度。设为 `False` 时由宿主调用 `recover()`。 |
| `context_window_tokens` | `int` | 否 | 模型上下文窗口。省略时 SDK 使用保守的上下文预算。 |
| `max_context_bytes` | `int` | 否 | 单份原生 trace 的字节配额，独立于普通附件和 Recipe 包的配额。 |

本期模型调用范围为支持工具调用的 OpenAI-compatible Chat Completions。创建时锁定本次 Execution 的模型及端点。模型凭证及附加请求头值通过进程间管道交给对应的 Worker，在进程内存中使用。SQLite 保存模型、端点和凭证作用域。凭证由每个 Client 单独提供。

```python
from cosens.rsi import RSIClient

gateway_config = {
    "model": gateway_model,
    "base_url": gateway_base_url,
    "api_key": access_token,
    "headers": {"X-Friday-Auth-Type": "access_token"},
    "credential_scope": gateway_credential_scope,
    "auto_start": True,
}
client = RSIClient(data_dir=account_data_dir, config=gateway_config)

custom_client = RSIClient(
    data_dir=account_data_dir,
    config={
        "model": custom_model,
        "base_url": custom_base_url,
        "api_key": custom_api_key,
        "headers": custom_headers,
        "credential_scope": custom_credential_scope,
        "auto_start": True,
    },
)
```

## 2. 创建 Execution

`execution_create(*, evidence, recipe_reference_paths, idempotency_key) -> dict`

| 参数 | 类型 | 约定 |
| --- | --- | --- |
| `evidence.context_path` | `str` | 截至材料截止回复的完整原生 trace 文件路径。 |
| `evidence.files` | `list[dict]` | 每项含 `path: str` 和 `role`。角色为 `input`、`artifact`、`reference` 或 `feedback`。无附加文件时传 `[]`。 |
| `recipe_reference_paths` | `list[str]` | 当前 thread 截至材料截止回复实际使用过的各版本 `RECIPE.md` 的本地路径，供 RSI 学习参考。无已用 Recipe 时传 `[]`。 |
| `idempotency_key` | `str` | 本次提交的稳定非空标识。 |

- SDK 在受理时保存 trace、附件和实际使用的 Recipe 包快照。`recipe_reference_paths` 中每项指向独立版本目录内的 `RECIPE.md`，快照保留包内脚本、模板和相对路径。包缺失、资源不可读取或引用越出包根目录时返回输入错误。
- 测试在独立工作区使用新生成的 Recipe 和本轮 `input`。原生 trace、确认产物、参考、反馈和已用 Recipe 包供学习阶段读取。
- 每个新请求在同一事务中分配 SDK Recipe ID 和 Execution ID。首次受理即返回这两个 ID，虽然此时 `recipe_version_id` 为 `None`。相同请求和幂等键重试时复用 ID 和快照。相同键提交不同材料或模型绑定时抛出 `Conflict`。凭证轮换可沿用原键。

```python
run = client.execution_create(
    evidence={
        "context_path": native_trace_path,
        "files": [
            {"path": input_path, "role": "input"},
            {"path": artifact_path, "role": "artifact"},
        ],
    },
    recipe_reference_paths=[recipe_reference_path],
    idempotency_key=request_id,
)
execution_id = run["id"]
sdk_recipe_id = run["recipe_id"]
```

## 3. 查询 Recipe 和 Execution

| 签名 | 返回 |
| --- | --- |
| `recipe_list(*, query=None) -> list[dict]` | `query={"recipe_ids": [...]}` 按 SDK Recipe ID 查询，每个 Recipe 返回一行。 |
| `recipe_get(recipe_id, *, version) -> dict` | `version` 为 `"active"`、`"latest"` 或具体版本 ID。返回 Recipe 及 `selected_version`。选择器当前无版本时为 `None`，指定不存在或不属于该 Recipe 的版本 ID 时抛出 `NotFound`。 |
| `execution_get(execution_id) -> dict` | 返回指定执行详情。 |
| `execution_list(*, query=None) -> list[dict]` | `query={"execution_ids": [...]}` 按 Execution ID 查询，返回字段与 `execution_get()` 一致。 |

`recipe_get()` 顶层返回 `id`、`name`、`created_at`、`updated_at`、`archived_at`、`latest_version_id`、`active_version_id`、`latest_qualified_version_id` 和 `selected_version`。`revision` 和 `description` 已从 Recipe 顶层移除。

按 ID 查询时，重复 ID 去重，未知 ID 跳过，空数组返回 `[]`。省略筛选时，`recipe_list()` 返回完整展示列表，`execution_list()` 返回全部未归档执行。按明确 ID 查询 Execution 时可读取匹配的已归档记录。Execution 列表按创建时间从旧到新排列。

| Recipe 列表字段 | 类型 | 含义 |
| --- | --- | --- |
| `recipe_id` | `str \| None` | SDK Recipe ID。历史未绑定的展示行可为 `None`。 |
| `execution_ids` | `list[str]` | 关联的未归档执行，待处理执行优先。 |
| `name` | `str` | 展示名称。 |
| `status` | `str` | 展示状态，可取 `active`、`inactive`、`queued`、`analyzing`、`generating`、`testing`、`needs_clarification`、`needs_credentials`、`passed`、`test_failed`、`unverifiable`、`completed`、`failed` 或 `cancelled`。 |
| `available_version_id` | `str \| None` | 当前可用版本 ID。 |
| `created_at`、`updated_at` | 数值 | Unix 秒时间戳。 |

| Execution 详情字段 | 含义 |
| --- | --- |
| `id`、`name`、`created_at`、`updated_at` | ID、展示名称、创建和更新时间。时间为 Unix 秒。 |
| `status` | `queued`、`running`、`needs_clarification`、`needs_credentials`、`completed`、`failed` 或 `cancelled`。 |
| `stage`、`progress_percent`、`input_revision` | 当前阶段、0–100 的整数进度、输入版本。 |
| `recipe_id`、`recipe_version_id`、`rubrics_version_id` | 关联 SDK Recipe、候选版本和 Rubrics 版本。候选和 Rubrics 产生前各自为 `None`。历史未绑定记录的 `recipe_id` 可为 `None`。 |
| `base_execution_id` | 历史续接记录的父 ID。按本契约新建和原 ID 续跑时为 `None`。 |
| `evidence` | 文件快照元数据列表，每项包含 `path`、`role`、`locator`、`sha256` 和 `size`（字节）。 |
| `questions`、`answers` | 当前问题列表，以及按 `问题 ID:问题版本` 索引的答案字典。 |
| `test_results` | 测试记录及各评分项，按 `input_revision` 区分各轮输入。 |
| `result` | 未完成时为 `None`。完成后包含 `test_status`（`passed`、`failed` 或 `unverifiable`）、`provisional` 和 `published`。 |
| `error` | 无错误时为 `None`。错误包含 `code`、`type`、`stage`，有上游状态时还包含 `upstream_status`。 |

```python
recipes = client.recipe_list(query={"recipe_ids": [sdk_recipe_id]})
associated_ids = recipes[0]["execution_ids"] if recipes else []
executions = client.execution_list(query={"execution_ids": associated_ids})

pending_questions = [
    {"execution_id": execution["id"], "question": question}
    for execution in executions
    if execution["status"] == "needs_clarification"
    for question in execution["questions"]
    if f"{question['id']}:{question['revision']}" not in execution["answers"]
]
```

问题项包含 `id`、`revision`、`question`、`assumption` 和 `options`。选项包含 `id` 和 `label`。答案项包含 `question`、`question_revision`、`selected_option_ids`、`selected_options` 和 `text`。Desktop 可用 `execution_get()` 读取详情并回显已提交答案。

## 4. 逐题回答并在原 Execution 续跑

`execution_clarification_answer(execution_id, *, question_id, question_revision, answer) -> dict`

`question_id` 和 `question_revision` 取自问题的 `id` 和 `revision`。`answer` 传一个非空字符串或非空选项 ID 列表。分析需要澄清时，执行进入 `needs_clarification`，Worker 释放调度资源。

| 情况 | 处理结果 |
| --- | --- |
| 仍有其他问题 | 保存答案。回执包含 `execution_id`、`remaining_questions` 和 `resumed=False`。 |
| 首次受理最后一题 | 同一事务保存答案、递增 `input_revision` 并将原 Execution 入队。回执为 `remaining_questions=0`、`resumed=True`。 |
| 重试已受理的相同答案 | 返回原回执。选项按集合比较，该重试只确认原受理结果。 |
| 重答同一版本但内容不同，或提交未受理的过期版本 | 抛出 `Conflict`，可重新查询当前问题。 |

```python
receipt = client.execution_clarification_answer(
    question_execution_id,
    question_id=question["id"],
    question_revision=question["revision"],
    answer=[selected_option_id],
)
```

`resumed=True` 表示原 Execution 已重新入队。后续澄清沿用同一 Execution ID，并返回新问题版本。

## 5. 凭证补充和 Worker 恢复

`recover(*, query=None) -> dict` 接受 `query={"execution_ids": [...]}`。省略筛选时，恢复当前目录内作用域、模型和端点均匹配的可恢复执行。空数组不触发调度。回执包含 `started: int`，旧租约尚未到期、已安排到期接管时还包含 `scheduled=True`。

上游拒绝凭证（例如 HTTP 401）或重启后缺少凭证时，原任务进入 `needs_credentials`。Friday 使用同账号、同凭证槽的新凭证创建 Client，恢复原任务的检查点。显式恢复与当前 Client 绑定不匹配的任务时，SDK 抛出错误，`code` 为 `credential_scope_mismatch`。等待澄清的任务在答完后方可入队。

```python
refreshed_client = RSIClient(
    data_dir=account_data_dir,
    config={**gateway_config, "api_key": refreshed_access_token},
)
recovery = refreshed_client.recover(query={"execution_ids": [execution_id]})
```

升级前先停止旧 Worker。已生成的历史记录和版本仍可按原 ID 查询。在途任务的账号凭证迁移由原宿主确认。SDK 恢复时按已保存的绑定校验。

## 6. 长 trace 读取

Server 传完整原生 trace，SDK 流式快照并计算哈希。超出 `max_context_bytes` 配额时受理失败，错误码为 `invalid_argument`。

Worker 内部工具 `read_evidence(locator, cursor=...)` 返回本段文本、证据定位符和 `next_cursor`，末段的游标为 `None`。游标绑定同一快照，读取保持 UTF-8 字符完整，并在有记录分隔符时优先沿记录边界分段。SDK 根据模型预算处理到文件末尾，再汇总带原文位置的学习结果。

## 7. 完成回执和云端交接

Friday Server 轮询本次 Execution。执行完成、测试通过、无待澄清问题且本地发布成功后，使用本次 Execution 的 `recipe_id` 和 `recipe_version_id` 精确读取产物。

```python
current = client.execution_get(execution_id)
result = current["result"] or {}
has_pending_questions = any(
    f"{question['id']}:{question['revision']}" not in current["answers"]
    for question in current["questions"]
)
version_id = current["recipe_version_id"]

if (
    current["status"] == "completed"
    and result.get("test_status") == "passed"
    and result.get("published") is True
    and not has_pending_questions
    and current["recipe_id"] == sdk_recipe_id
    and version_id
):
    version = client.recipe_get(sdk_recipe_id, version=version_id)["selected_version"]
    if version is None or version["id"] != version_id:
        raise RuntimeError("SDK Recipe version unavailable")

    completion = {
        "execution_id": current["id"],
        "recipe_id": sdk_recipe_id,
        "recipe_version_id": version_id,
        "content": version["content"],
    }
```

版本内容包含 `title`、`applicability` 和 `instructions`。Friday 用它组装 `RECIPE.md`。当 `recipe_authoring_tasks.target_recipe_id` 为空时新建云端 Recipe 并安装，有值时更新指定 Recipe 并等待用户确认应用。`available_version_id` 用于展示或选择当前可用版本。

## 8. 其他本地接口

| Python 签名 | 用途 |
| --- | --- |
| `execution_append_evidence(execution_id, *, evidence)` | 给进行中的执行补充材料，返回更新后的详情。 |
| `execution_cancel(execution_id)` | 取消未终结的执行，返回更新后的详情。 |
| `recipe_test(*, execution_id, recipe_id, version_id, rubrics_version_id)` | 安排指定版本测试。Friday authoring 流程使用自动测试。 |
| `recipe_version_set_archived(recipe_id, *, version_id, archived)` | 归档版本，或恢复最新通过验证的版本。 |
| `rubrics_list(*, offset, archived)` | 每页 50 条，`offset` 从 0 开始。 |
| `rubrics_get(rubrics_id, *, version)` | 用 `"latest"` 或版本 ID 读取评分标准版本。 |
