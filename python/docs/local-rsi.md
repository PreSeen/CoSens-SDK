# CoSens 本地 RSI SDK 接口文档

适用于 `RSI_SDK_v2` 本地预览，TS 版本为 `0.5.4-rsi.21`。TS 从 `cosens-sdk/rsi` 导入，Python 从 `cosens.rsi` 导入。本版公开接口只接收必填参数。`null`（Python 为 `None`）用于明确表示首次学习。

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
| Recipe | `recipe_list` | 无 | 返回未归档 Recipe 和首次学习进度，用于 Agent 面板。 |
| Recipe | `recipe_get` | `recipe_id`, `version` | `version` 传 `"active"`、`"latest"` 或版本 ID，返回选中版本。 |
| Recipe | `recipe_test` | `execution_id`, `recipe_id`, `version_id`, `rubrics_version_id` | 在活动 Execution 内安排指定版本测试。 |
| Recipe | `recipe_version_set_archived` | `recipe_id`, `version_id`, `archived` | `true` 归档指定版本，`false` 恢复最新通过验证的版本。返回 `recipe_id`、`version_id`、`archived`。 |

`recipe_list()` 中的 `available_version_id` 指向当前可用版本。Agent 将 `recipe_id` 和 `available_version_id` 传给 `recipe_get`。手动归档当前版本时，条目保留，`archived=true`、`available_version_id=null`，可用 `latest_qualified_version_id` 恢复。首次学习或验证未通过时，`available_version_id` 也可能为空，此时用 `archived` 区分。

在没有未决澄清时，上一个可用版本保留到编号更高的版本验证通过。切换和旧版归档在同一个事务完成。旧版可按 ID 读取，`selected_version.archived` 表示所查版本的归档状态。恢复仅支持最新通过验证的版本。手动归档只作用于指定版本，更新的版本通过验证后仍会自动可用。

对象分页固定每页 50 条。`offset` 从 0 开始，`archived=false` 查未归档对象，`true` 查已归档对象。

### Execution 进度

`execution_get` 和 `execution_list` 返回整数 `progress_percent`（0–100）。排队为 0%，分析为 10%。默认两轮设置下，首轮生成、测试和评分为 25%、40%、55%，第二轮为 65%、75%、90%。进度表示工作阶段，最终结果保存后为 100%。测试结果见 `result.test_status`。

执行失败或取消时保留已记录的进度。Worker 恢复同一输入版本时，进度保持单调。追加证据、接受澄清或安排重测使 `input_revision` 递增时，新一轮进度随该输入版本重新计算。界面可在存在待回答的澄清问题时优先显示「待澄清」。

### Execution 证据和测试

- `evidence` 由必填的 `context_path` 和 `files` 组成。无附加文件时传 `[]`。每个文件都需要 `path` 和 `role`，角色为 `input`、`artifact`、`reference` 或 `feedback`。
- SDK 在创建 Execution 时复制文件快照。详情的 `evidence[]` 返回文件列表，包含角色、哈希和定位符。旧记录沿用原快照读取。
- 宿主在取得用户交付确认后调用接口。SDK 使用 `idempotency_key` 记录本次提交。
- SDK 自动用 Recipe 重跑当前任务。创建时标为 `input` 的文件同时用于学习和测试。测试在独立工作区生成新产物，再按 Rubric 评分。上下文、历史参考（`reference`）、确认产物（`artifact`）和反馈只用于学习。该得分衡量当前任务能否复现。
- `base_execution_id` 填已结束的 Execution ID。SDK 自动关联该执行生成的 Recipe 版本（如有）。首次学习传 `null`（Python 为 `None`）。执行仍在进行时，使用 `execution_append_evidence` 追加证据。
- `idempotency_key` 是本次提交的稳定非空字符串。同一操作重试保留原键和输入。旧接口已经提交的执行可按 ID 查询或恢复，使用旧提交的幂等键提交新格式会报冲突。
- Worker 自动提炼并锁定 Recipe 名称、生成 Rubrics、保存候选及测试结果。自动学习最多两轮。

### 更新和澄清参数

- `execution_append_evidence` 使用与创建相同的 `evidence` 结构，只提交新增内容。新证据递增输入版本并触发重新分析，测试继续使用创建时的输入范围。重试相同证据批次会复用原快照。
- `question_id` 和 `question_revision` 来自 `execution_get().questions`。 `answer` 为选中的选项 ID 数组或非空文本。已完成执行如果返回 `continuation_execution_id`，宿主应跟踪该子执行。

## 运行管理

| 方法 | 参数 | 用途 |
| --- | --- | --- |
| `recover` | 无 | Agent 启动时调用一次。返回 `started`，旧租约尚未过期时可返回 `scheduled=true`。 |
| `close`（TS） | 无 | 释放宿主持有的 SQLite 连接。 |

TS 需等待 `execution_create`、`execution_append_evidence`、`execution_clarification_answer`、`recipe_test` 和 `recover` 返回。Python 接口按同步方式调用。
