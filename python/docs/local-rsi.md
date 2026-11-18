# RSI SDK V2：本地嵌入式算法

本版在 `cosens.rsi` 中实现 Material、Execution、Rubrics、Recipe 四类对象。Topic、Task、Event 不属于这套新接口。

Hook → Python SDK → SQLite 持久化队列 → 按需后台进程 → LangGraph 学习流程 → LangChain 工具调用 Agent。

本地没有算法 HTTP 服务、端口、Redis 或常驻调度服务。Hook 返回后，独立后台进程继续运行；队列排空后进程退出。宿主退出会杀死全部子进程的容器或沙箱环境，需要宿主通过 `recover()` 或系统任务重新启动 Worker。宿主启动时调用一次 `recover()` 即可。如果旧租约仍占用并发名额，返回 `scheduled: true` 表示后台进程会等待租约释放或过期后接管。

## 安装与初始化

```bash
pip install -e ./python
```

```python
from cosens.rsi import RSIClient, LocalConfig, ModelConfig

rsi = RSIClient(
    data_dir="/home/me/.cosens/rsi",
    config=LocalConfig(
        model=ModelConfig(
            model="your-local-tool-calling-model",
            base_url="http://127.0.0.1:8000/v1",
            api_key_env="RSI_MODEL_API_KEY",
        ),
        max_executions=2,
        max_model_requests=1,
    ),
)
```

模型必须支持兼容 OpenAI 的 Chat Completions 和工具调用。宿主提供模型地址和模型名。密钥通过指定环境变量传递给后台进程，不写入数据库、检查点、参数或提示词。不需要认证的本地模型也应设置一个非敏感占位值。SDK 不读取 `.env`。

同一 `data_dir` 的所有会话共享配置、队列和并发预算；首次初始化保存配置，后续可省略 `config`。不同配置访问同一目录会抛出 `Conflict`，防止会话各自覆盖运行设置。请在初始化时选定容量。`auto_start=False` 可关闭自动派发，由宿主明确调用 `run_pending()` 或 `recover()`。

宿主可设置 Exa 密钥环境变量 `COSENS_RSI_WEB_SEARCH_API_KEY`，让 Recipe 生成阶段按需调用 `web_search`。可用 `COSENS_RSI_WEB_SEARCH_ALLOWED_DOMAINS` 和 `COSENS_RSI_WEB_SEARCH_BLOCKED_DOMAINS` 配置以逗号分隔的域名。搜索结果保存在生成轨迹中，测试和评分继续使用原有文件权限。

## Agent 的完整交互流程

```text
宿主 Agent 开始任务
→ recipe_list(q=..., active_only=True)
→ recipe_get(recipe_id) 获取启用版本
→ 宿主自行选择是否使用、执行任务、收集用户反馈
→ 宿主交付产物并取得确认
→ 宿主把完整上下文写入一个文件，列出输入、参考文件和产物路径
→ material_create(manifest=...) 保存一个证据包
→ execution_create(..., policy=...) 持久化并派发，立即返回 execution_id

后台 RSI Agent
→ 通过 read_evidence 自己读取证据文件
→ 判断必要信息是否充分，结合任务、反馈和轨迹形成 Rubrics
→ 只有必要信息缺失时形成 Hypothesis 和选项问题，记录在 Execution
→ 按明确标记的假设继续生成 Recipe 候选版本
→ 使用该 Recipe 在新工作目录实际执行 test_task
→ 先检查真实文件，再由独立上下文按固定 Rubrics 评分
→ 未通过时在 max_rounds 内修改 Recipe 并重新测试
→ 保存 passed / failed / unverifiable；Execution 结束
→ 仅测试通过、无未答必要问题且 auto_activate=True 时尝试启用

宿主轮询 execution_get(execution_id)
→ 有必要问题时由宿主 UI 展示选项
→ execution_clarification_answer(...)
→ 运行中：递增输入版本，在安全边界重新分析
→ 已完成：原结果保留，原子创建子 Execution
```

任务开始和业务执行阶段的判断由宿主 Agent 完成。本地 SDK 不弹窗，也不在后台进程中读取终端输入。`artifact_confirmed=True` 和 `confirmation_ref` 是可信宿主的明确授权声明，不能替代宿主实际取得用户确认。

## 对象定义与属性

各对象共有 `id`、`revision`、`created_at`、`updated_at`、`archived_at`。时间是 UTC Unix 秒；返回值为可 JSON 序列化的字典。所有 `list` 支持 `limit=50`（1–200）、`offset=0`、`archived=False`；排序为创建时间和 ID。没有删除接口。

| 对象 | 定义 | 主要属性 |
|---|---|---|
| Material | 宿主提供的一次不可变学习证据包 | `manifest`、`files`、`status=ready`、`name` |
| Execution | 一次完整的学习、生成、测试和迭代流程 | `material_ids`、`parent_execution_id`、`base_recipe_version_id`、`source_session_id`、`input_revision`、`policy`、`status`、`stage`、`questions`、`answers`、`test_results`、`result`、`error` |
| Rubrics | 可复用的评价标准及其不可变版本 | `name`、`description`、`latest_version_id`；版本含 `criteria`、`pass_threshold` |
| Recipe | 可复用的方法及其不可变版本 | `name`、`description`、`latest_version_id`、`active_version_id`；版本含 `title`、`applicability`、`instructions` |

版本记录含 `id`、`object_id`、`number`、`content`、`provenance`、`created_at`。版本只能追加；元数据更新不修改历史版本。

### Material

```python
material = rsi.material_create(
    manifest={
        "context_path": "/workspace/context.md",
        "source_session_id": "host-session-1",
        "source_task_id": "host-task-1",  # 外部引用，不创建 Task 对象
        "capture_boundary": "delivery-1",
        "files": [
            {"path": "/workspace/data.csv", "role": "input", "description": "测试输入"},
            {"path": "/workspace/report.md", "role": "artifact", "description": "已确认产物"},
        ],
    },
    idempotency_key="material:session-1:delivery-1",
)
```

`context_path` 文件由宿主写入完整目标、约束、对话、工具调用过程和结果、反馈、交付状态。SDK 不负责读取特定宿主的 session 私有格式。`files.role` 为 `input/artifact/reference/feedback`。每个文件会复制为只读快照，保存原路径、实际 SHA-256、字节数和 `material_id/index` 定位符；源文件随后变化不影响学习。

反馈也使用 Material：`manifest.feedback_context` 可记录 `target_type`、`target_id`、`source`；原始反馈写进上下文或 `feedback` 文件。运行中用 `execution_update` 加入；终态后创建子 Execution。没有 `execution_feedback_record`。

| 接口 | 参数 | 返回与使用环节 |
|---|---|---|
| `material_create` | `manifest`, `idempotency_key=None` | 创建一次完整证据包，返回 Material；交付后或有新反馈时调用 |
| `material_get` | `material_id` | 返回清单、文件快照和哈希；查看学习证据 |
| `material_list` | 分页参数 | 返回 Material 列表 |
| `material_update` | `material_id`, `name=None`, `description=None`, `archived=None`, `expected_revision=None` | 仅更新元数据、归档或恢复，不改变证据 |

默认单文件上限 4 MiB、包上限 32 MiB；初始化配置可调整。Agent 每次读取最多返回 100000 字符，并明确标注截断。支持 UTF-8 文本，以及 DOCX/XLSX 的基础 XML 文本提取（不计算公式，不完整保留表格/版式语义）；其他二进制需宿主提供文本提取文件。超限或缺失文件明确报错。

### Execution

```python
execution = rsi.execution_create(
    material_ids=[material["id"]],
    source_session_id="host-session-1",
    idempotency_key="execution:session-1:delivery-1",
    policy={
        "artifact_confirmed": True,
        "confirmation_ref": "host-user-confirmation-123",
        "auto_activate": False,
        "max_rounds": 2,
        "test_task": {
            "instruction": "读取输入 CSV，生成 summary.md，写明记录数。",
            "input_locators": [material["files"][1]["locator"]],
            "shared_reference_locators": [],
            "checks": [{"kind": "exists", "path": "summary.md"}],
        },
    },
)
```

`status` 为 `queued/running/completed/failed/cancelled`。`stage` 为 `queued/analyze/generate/test/evaluate/done/failed/cancelled`。`completed` 只表示流程结束，是否验证成功必须读取 `result.test_status`。候选 Recipe 可能是 `provisional`；不能仅凭 Execution 结束便采用。

| 接口 | 参数 | 返回与使用环节 |
|---|---|---|
| `execution_create` | `material_ids`, `policy`, `rubrics_version_id=None`, `base_recipe_version_id=None`, `parent_execution_id=None`, `source_session_id=None`, `idempotency_key=None` | 返回排队 Execution；持久化后按需启动后台进程 |
| `execution_get` | `execution_id` | 返回状态、问题、测试轮次和结果；宿主轮询 |
| `execution_list` | 分页参数 | 查看历史执行 |
| `execution_update` | `execution_id`, `material_ids=None`, `archived=None`, `expected_revision=None` | 向活动执行追加证据并递增输入版本，或归档/恢复；终态不接受新输入 |
| `execution_clarification_answer` | `execution_id`, `question_id`, `question_revision`, `selected_option_ids`, `text=None`, `idempotency_key=None` | 返回 `execution_id` 和可空 `continuation_execution_id`；回答必要问题 |
| `execution_cancel` | `execution_id` | 协作取消排队/运行任务；阻止后续提交和启用，保留已发生的本地工作 |

`policy.test_task` 缺失、没有实际输出或评价无法验证时，测试为 `unverifiable`，不能启用。确定性检查支持 `exists/text_equals/text_contains/sha256`，路径必须位于测试输出目录内。`max_rounds` 限制 1–10，模型请求有超时和工具循环步数上限。

宿主通过 `test_task.input_locators` 指定本轮测试输入，并用 `shared_reference_locators` 授权测试可读的共享资料。两者都使用本次 Execution 所附 Material 返回的完整文件定位符，角色必须分别为 `input` 和 `reference`。省略名单时对应文件默认不可读，已有执行也遵循这个默认值。历史文件和已确认产物留给学习阶段，本轮测试输入留给测试执行和评分。评分还可读取本轮测试产物和执行轨迹。

问题具有 `id/revision/question/options/assumption`，选项含 `id/label`。只有必要缺口才创建问题，其他情况不制造 Hypothesis。版本过期或试图覆盖已接受答案返回 `Conflict`；同一答案重复提交不创建重复子执行。修改已接受答案使用新反馈 Material。失败/取消后的回答会记录，但不自动续跑；明确调用 `execution_create(parent_execution_id=...)`。

证据引用的 `material_id` 是材料包 ID，`locator` 是完整文件定位符（`material_id/file-index`），两者不可混用。模型可见清单分别提供这两个字段；Schema 校验格式与归属，执行前进一步检查引用是否属于授权材料清单。

跨 session 可基于同一个活动 Execution 追加新材料；终态需要子 Execution。`parent_execution_id` 记录历史关系，`base_recipe_version_id` 指定延续的 Recipe 版本，`material_ids` 明确列出获准使用的全部材料，不隐式读取其他 session。

### Rubrics

每条 criterion 含 `id`、`description`、`source`、`evidence`、`required=True`。`source` 为 `task/user_feedback/trajectory/agent_inference`；前三者需要来源定位。证据引用是 `{material_id, locator}`。`pass_threshold` 默认 0.8。任务初始化和反馈/轨迹修正可共同形成标准；同一 Execution 的测试轮次固定标准版本。修改标准必须追加版本，不能把不同标准的分数当成同一组比较。

| 接口 | 参数 | 返回与使用环节 |
|---|---|---|
| `rubrics_create` | `name`, `content=None` | 返回 Rubrics；宿主预置标准，或后台学习自动创建 |
| `rubrics_get` | `rubrics_id`, `version_id=None`, `include_content=True` | 返回对象及 `selected_version`，默认最新版本 |
| `rubrics_list` | 分页参数 | 查看标准集合 |
| `rubrics_update` | `rubrics_id` 和元数据更新参数 | 更新名称、说明，归档/恢复 |
| `rubrics_version_create` | `rubrics_id`, `content`, `expected_revision=None`, `idempotency_key=None` | 追加不可变标准版本 |
| `rubrics_version_list` | `rubrics_id` 和分页参数 | 查看历史版本 |

### Recipe

| 接口 | 参数 | 返回与使用环节 |
|---|---|---|
| `recipe_create` | `name`, `content=None` | 新建独立 Recipe，初始不启用；通常由学习流程创建 |
| `recipe_get` | `recipe_id`, `version_id=None`, `version_selector=None`, `include_content=True` | 返回对象及 `selected_version`；默认启用版本，无启用版本时为 `None`，不回退到最新候选；`version_selector="latest"` 查看候选 |
| `recipe_list` | `q=None`, `active_only=False` 和分页参数 | 名称、说明、启用版本内容的本地文本搜索；宿主开始任务时选用 |
| `recipe_update` | `recipe_id` 和元数据更新参数 | 更新名称、说明，归档/恢复 |
| `recipe_version_create` | `recipe_id`, `content`, `expected_revision=None`, `idempotency_key=None` | 追加候选版本，不绕过测试启用 |
| `recipe_version_list` | `recipe_id` 和分页参数 | 查看历史版本 |
| `recipe_test` | `execution_id`, `recipe_id`, `version_id`, `rubrics_version_id`, `material_ids=None` | 在活动 Execution 内请求实际测试已有版本，返回排队信息；不新建 Execution；结果在 `execution_get.test_results` |
| `recipe_activate` | `recipe_id`, `version_id`, `expected_revision` | 启用已通过测试、无未决必要问题的版本；CAS 防覆盖；`version_id=None` 清空启用指针 |

`recipe_test` 指定测试请求后，该 Execution 测试所选现有版本，不再自动修改它；修订需另一次学习流程。重复相同测试请求不重复递增输入版本。默认学习流程内部调用同一测试阶段，失败时可生成新版本重测，不需要宿主逐步驱动。

`recipe_get` 的 `version_id` 与 `version_selector` 互斥。本版提供本地文本检索。

## 并发、恢复与边界

同一 SQLite 使用 WAL、短事务和 busy timeout。默认最多 2 个 Execution，同时模型请求上限默认 2；本地模型较小时建议设 1。超额任务落盘排队。每个 Execution 独立工作目录和 LangGraph thread；宿主 session ID 仅为来源引用。

进程槽、任务租约和心跳跨 Hook 共享。数据库原子领取任务，租约过期后新 Worker 取得递增 fencing token；旧 Worker 不能提交、启用或覆盖新 Worker 检查点。模型调用期间不持有业务事务。不同 data_dir 是独立实例，不共享并发预算。

`execution_create`、`material_create`、版本追加和答题支持幂等键；同键不同参数抛出 `Conflict`。新交付必须用新的幂等键。取消不会回滚已经发生的模型请求；机器崩溃可能导致某次模型请求或未完成测试重做，不能承诺外部调用 exactly-once。

```python
rsi.recover()                  # 宿主启动、机器重启后，派发排队/过期任务
result = rsi.wait(execution_id, timeout=300)  # 可选轮询助手，超时不取消任务
```

也可以在与 Hook 相同的 Python 环境运行 `python -m cosens.rsi.worker --data-dir /home/me/.cosens/rsi`，处理队列后退出。检查点本身不能在机器重启后自动唤醒进程，宿主需要调用恢复入口。

测试执行器只提供受限的 `read_evidence/write_output/read_output`，不会启动 shell、浏览器或任意网络工具。适用于可通过输入文件和文件产物复现的任务；涉及外部系统、编译、浏览器等任务需要扩展 Runtime 工具能力并提供受控环境，不能把没有做过的测试标为通过。测试执行不能读取宿主产物/上下文，评价使用独立 Agent 上下文和实际输出。

评价 Agent 同时获得本次 Recipe 内容及只读执行轨迹文件，可以核查方法约束和工具使用证据；确定性文件检查由 SDK 计算，不由模型自行宣告通过。

本地文件和轨迹保留在指定目录；学习图明确关闭继承自宿主的远端 LangSmith tracing。配置模型地址如果是远端服务，模型调用中的选定证据自然会发送到该地址，部署方应选择符合材料使用范围的模型。宿主自行安装的外部 instrumentation 仍需宿主管理。

`RSIError` 子类 `NotFound/Conflict/InvalidState` 提供稳定 `code`。异步运行错误保存在 `Execution.error`，仅记录错误类型与通用错误码，避免把上游含凭据的异常字符串落盘。原始模型和工具消息保存在本地阶段轨迹中，供宿主检查。

模型未产生结构化结果（例如工具参数不是合法 JSON）时，SDK 保留已读取的上下文，最多请求两次纠正；仍不符合 Schema 时返回 `invalid_model_output`，不创建虚假的 Recipe。HTTP 402/429/5xx 分别记录 `model_payment_required/model_rate_limited/model_unavailable` 和 `upstream_status`，不保存上游响应正文或凭据。

## 代码组织与验证

`client.py` 是 SDK 入口；`engine.py` 是学习图；`runtime.py` 是 LangChain Agent 与文件工具能力；`store.py` 是本地 SQLite 队列和业务存储适配；`worker.py` 是按需进程入口。

[可运行的宿主接入示例](../examples/05_rsi_sdk_v2.py)。完整测试：

```bash
pip install -e './python[test]'
python -m pytest python/tests/test_rsi_v2.py python/tests/test_rsi_v2_background.py -q
```

测试覆盖真实 LangGraph 图、SQLite、文件测试、版本启用、澄清、恢复和并发。后台测试启动 4 个独立 Hook 进程，使用一个本机 OpenAI-compatible **协议测试服务器** 驱动真实 LangChain 工具循环，验证 Hook 退出后的继续执行、2 个流程并发上限和 1 个模型请求上限。它不证明某个真实模型的学习质量；部署模型需另做真实任务验收。
