# CoSens Python SDK 三种接入方式设计

状态：设计稿，待确认；本文件不代表 SDK 已实现。远端基准地址：`http://27.221.10.54:41011`。

参考契约：[RSI_WORKFLOW_API(1).md](/Users/a1111/Desktop/RSI_WORKFLOW_API(1).md)，尤其是第 3、4.3、5、6、8 节。本设计以用户后续澄清为准，覆盖参考契约第 1.1、2、4.1、4.2 节中的旧 SDK 入口定义。2026-10-26 只读查询远端 `/health/live` 返回 200；`/openapi.json` 标为 `1.0.0`，与附件 2.1.0 并不一致。下文将标明哪些接口已在该地址公开，哪些仅见于附件；本仓库当前没有 SDK 源码。

## 1. 目标与边界

CoSens 在用户原有的 OpenAI 调用旁收集被授权的对话与执行证据，调用 RSI 服务准备 Recipe/Memory、记录 Run 结果，并在获得真实反馈或明确改进原因后启动改进。Agent/工具仍在使用方进程中执行；RSI 服务只负责准备、记录、学习、验证与发布。

Python 首版提供三层入口：

1. **透明调用**：`cosens(OpenAI(), mode="auto")` 返回覆盖原始 OpenAI client **全部公开功能**的兼容代理；模型对话请求可触发 CoSens，应用不显式调用 CoSens 的 `run`。`auto` 由模型判断该次任务是否需要提交 RSI Run。
2. **显式运行**：`cosens(agent, mode="disable").run(agent, user_input, session=...)`。`disable` 在这个入口关闭隐式触发判断；显式 `run` 本身是启动 RSI 的指令。SDK 在这一次 `run` 中直接拼接参考契约第 4.3 节的原子接口，不调用协调器。
3. **原子调用**：`CoSensClient(...).atoms.*` 暴露参考契约第 6 节的九个原子操作，并对当前远端增加 `session_events`；应用自行决定步骤与执行时机。

三个入口共用认证、传输、数据模型、幂等键、Session 消息游标、恢复记录与错误模型。项目根目录按语言划分为 `python/` 和 `typescript/`；当前仅整理文档，不开发任何 SDK 代码。

本设计不改变 RSI 算法、Memory 内部接口、模型工具执行权限、Agent 原始业务结果或服务端发布规则。`submit` 只记录结果，不能被解释为验收成功；`validated` 也不会自动发布。

## 2. 方案选择与关键取舍

| 候选方案 | 适用性与取舍 | 决定 |
| --- | --- | --- |
| 只包装 `agents.Runner` | 能准确看到一个 Agent turn、工具和 Session，但不满足第一种“封装原始 OpenAI SDK、拦截 `responses.create`”的要求 | 不作为第一种；用于第二种 |
| 只包装 `openai.OpenAI` | 可覆盖原始客户端全部公开功能；单个 API 调用无法天然知道应用工具何时执行、整轮任务何时结束 | 作为第一种，明确运行边界 |
| 一个工厂按对象类型分派到两个代理，并共用原子客户端 | 同时满足透明接入和完整 Agent 编排；维护两套边界采集器 | 采用 |

`cosens()` 不修改传入对象，也不对 `openai` 或 `agents` 做全局 monkey patch。再次包装同一代理且配置一致时返回原代理；配置冲突时报错，避免一条响应被重复上报。

## 3. 公开 Python 用法

### 3.1 方式一：包装原始 OpenAI SDK

```python
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(
    native,
    mode="auto",
    decision_model="<configured-model>",
    session=session,  # 应用提供可读取的聊天历史
    base_url="http://27.221.10.54:41011",
)

response = client.responses.create(model="<application-model>", input="用户任务")
print(response.output_text)
```

公开入口至少接收 `OpenAI` 与 `AsyncOpenAI` 客户端；若传入 `AzureOpenAI/AsyncAzureOpenAI`，也必须保留其原有资源、认证与 API 版本语义。代理额外提供 `client.cosens.get_turn(turn_id)` 查询 RSI 状态。只构造 `client` 不发送请求。若应用持有可读取的 Session，显式交给 `cosens(..., session=session)` 或本次 scope；原始 `OpenAI` 客户端自身不提供 `agents.Session`，SDK 无法凭 client 推断之前的聊天消息。

**完整 SDK 兼容契约**：`cosens(client)` 返回的对象必须让原 client 的所有公开资源树、方法、属性和调用链继续可用，而不能只实现一个 `responses.create` 门面。包括 `responses`、`chat`、`completions`、`files`、`audio`、`images`、`embeddings`、`batches`、`models`、`moderations`、`vector_stores`、`fine_tuning`、`realtime`、`beta`，以及当前所安装 OpenAI SDK 版本新增的公开资源。正常委托调用须保留位置/关键字参数、默认值、返回模型、异常类型、请求选项、重试、分页、文件与二进制传输、同步/异步和流式生命周期。`client.with_options(...)`/`copy(...)` 派生出的客户端仍保持 CoSens 包装及同一 Session 游标；`client.close()`、上下文管理器、异步上下文管理器按原生客户端语义释放资源。`with_raw_response`、`with_streaming_response` 及资源下的同名调用链也必须可达，不能因代理少了一层属性而失败。通用 `client.get/post/...` 自定义请求同样透传。

实现上使用递归资源代理与显式的模型请求适配器；SDK 每次按原生资源对象动态委托，不能手写一个随 OpenAI SDK 更新而过期的固定方法清单。对需要 CoSens 的模型生成调用，在请求发出前和结果实际完成后挂钩；非对话资源仍原样调用，不凭文件上传、模型列表或管理操作创建 RSI Run。`responses.create`、`responses.parse/stream`、`chat.completions.create/parse/stream`、旧式 `completions.create` 及其 raw/streaming 变体属于需要逐一验证的生成调用入口。对输入或输出无法安全归一化的生成变体，调用保持原生可用，并明确记录 `rsi_capture=unsupported`，不能静默宣称已经上报。批量任务和 Realtime 长连接同样要保持原功能；它们的 RSI 边界需有独立适配规则，不能把建立连接或创建 Batch 当作一次完成的聊天 Run。

兼容目标以 SDK 明确支持的 `openai` Python 包版本为单位验收：记录该版本的公开资源/方法清单，逐项验证代理存在、签名可调用、原生返回和错误可传递；升级 `openai` 包时重复此检查。动态委托可让新增方法仍可调用，但未经适配与验收的新模型生成方法不得被标为“已完成 RSI 采集”。CoSens 的方法仅放在 `client.cosens` 命名空间，避免覆盖原生属性。

代理的“功能可用”不等于 Python 名义类型完全相同：包装现有实例后，`isinstance(wrapped, OpenAI)` 等身份检查可能不同。对此保留 `client.native` 访问原对象；任何通过 `native` 发起的调用都不会被 CoSens 拦截。若第三方库强制接收原始类实例，需要单独的兼容适配，不得宣称完全透明。

模型判断使用传给 `cosens()` 的 `decision_model`；启用 `auto/approval` 时它是必填配置，不猜测应用调用中的 `model` 是否适合分类。判断请求经**未包装的** OpenAI 客户端发出，要求结构化结果：`activate: bool`、`objective`、`task_family`、`reason`。这是一次额外模型调用，会增加费用和首次响应延迟。首版默认只给分类器当前请求中可见的最近用户文本与工具名称，截取上限 8,000 字符；不发送文件正文、工具输出、系统/开发者指令或完整历史。可以通过显式采集策略扩大范围，但必须由应用配置且仍受授权约束。建议默认决策超时 5 秒、输出上限 256 token，均可配置。原始调用中的 `store`、`metadata`、`tools` 等不被改写为分类器的工具权限。

当 `activate=false`，原生 `responses.create` 执行一次，不请求 CoSens，不创建 RSI Run，消息游标不前进。当 `activate=true`，SDK 读取该 Session 中**尚未收到远端确认**的历史消息，拼接原子流程并增量上报；已确认的历史消息不再上传。在原有 `instructions/input` 上附加有来源的 Recipe/Memory 片段，再执行原生调用；不得删除或重排原请求中的指令、工具与内容。若请求形态无法安全注入，必须在原生调用前报明确错误，而不是悄悄省略已准备的 Recipe/Memory。分类失败、超时或无法解析时，`auto` **不向 CoSens 提交**、原生调用继续，消息游标不前进，并通过本地状态和可选事件回调暴露 `decision_failed`；`approval` 则在原生执行前报 `RSIDecisionUnavailable`。透明入口的 `disable` 不调用分类器，也不请求 CoSens。

一条 `responses.create` 不等于完整 Agent turn。首版以“已收到最终 assistant 消息、没有待处理的 function call，且响应完整结束”为自动终态；该响应之后的用户新输入开始新 turn。若应用用 `previous_response_id` 或 `conversation` 续接模型与工具循环，代理用服务返回的响应 ID 和原生引用关联同一逻辑 turn。无法可靠关联时，不推断工具已执行或任务已完成；调用方可用可选 `client.cosens.scope(session_id=..., turn_id=...)` 显式给出边界。此 scope 不改变原生请求参数。仅有 function call、未见工具结果的响应保持 `awaiting_continuation`；若后续始终没有可关联调用，过期后标为 `incomplete`，不 submit。受限于原始 SDK 的可见性，应用在代理外执行的工具副作用、外部 Session 全量历史和人工审批，方式一不能保证自动捕获；需要完整轨迹时使用方式二。只有真正使用被包装 client 的请求会被拦截，其他 OpenAI client 不受影响。

所有原生流式调用保持事件次序和迭代/上下文管理器语义。对 `responses.create(stream=True)`，分类和准备发生在打开原生流之前；收到 `response.completed` 且对应逻辑 turn 已终结后才提交结果。其他生成流采用各自的真实完成事件，不拿首个 chunk 当终态。流取消、断线或只有工具请求时不伪造完成。异步版本遵循相同规则；不得把同步调用暗中切换成异步调用。原生异常原样传播，已产生的 RSI 资源与恢复信息保留。

### 3.2 方式二：显式 `run` 包装 Agents SDK

```python
from agents import Agent
from cosens import cosens

agent = Agent(name="assistant", model="<application-model>", tools=tools)
client = cosens(agent, mode="disable", base_url="http://27.221.10.54:41011")
result = await client.run(agent, user_input, session=session)
print(result.final_output)
```

`cosens(Agent)` 返回绑定该 Agent 的 `CoSensAgentRunner`；`run(agent, input, **native_options)` 是显式入口，并把 `session` 等原生选项交给 Agents SDK `Runner.run`。`mode="disable"` 只关闭**隐式的 prompt 触发**，不取消用户已经显式调用的 CoSens `run`。此入口必定尝试 RSI 原子流程，不经过决策模型。正常完成返回原生 `RunResult`，RSI 元数据通过 `client.cosens.get_turn(turn_id)` 获取。`run_sync`、`run_streamed` 按相同边界设计，不能伪造或替换原生工具调用、handoff、guardrail 与流事件。

每次顶层 `run` 只有一个 `turn_id` 和至多一个准备 Run；该 run 内的多轮模型调用、工具调用、handoff 与原生工具审批恢复都属于同一 turn。为本次调用克隆/组合有效指令并注入冻结的 Recipe/Memory，不改并发共享的 Agent 对象。进入原子流程前读取 `session.get_items()`（或宿主授权的等价读取接口），按第 5 节游标筛选增量消息。Session 中之前已经上报并确认的消息不再发；本次 `user_input` 若还未写入 Session，则作为本次任务目标使用，原生运行后再根据 Session 的实际新增项上报，不能靠同文本猜测它已经存在。

**一次显式调用的顺序**：读取 Session 与本地游标 → 上传本次任务需要的原始文本材料 `POST /v1/artifacts` → `POST /v1/runs/prepare` → `GET /v1/runs/{run_id}` → 把未确认的历史消息发送到 `POST /v1/runs/{run_id}/session-events` 并按 ACK 前进游标 → 原生 `Runner.run` → 持久化最终结果 → 把新增消息/工具结果通过同一事件接口增量上报 → 按需上传文本交付物 → `POST /v1/runs/{run_id}/submit` → 有真实反馈时 `POST /v1/runs/{run_id}/feedback` → 有真实反馈或明确改进原因时 `POST /v1/runs/{run_id}/improvements` → 需要观察时 `GET /v1/improvements/{job_id}`。所有 CoSens 请求由第三种的原子客户端发出；`client.run` 只是调用它们的本地编排器。管理员发布是独立动作，不属于 `client.run` 返回前的自动步骤。反馈接口可能已经自动创建 Job；此时复用返回的 Job，不再重复调用 improvements。

若增量消息发送失败，显式 `run` 在原生 Runner 启动前停止，并复用同一 Run 与事件 ID 重试；不推进游标。若 Agent 原生执行失败，保留原异常及 Run 状态，不假装已提交成功。若 Agent 完成后 CoSens 上报或提交失败，先保存原生结果并暴露 `completion_pending`；恢复时只对账和补交远端状态，不重跑 Agent。线上 `1.0.0` 的 artifact/submit/feedback 没有通用幂等 Header，响应结果不明时不能盲目重放：先 `GET Run` 检查是否已完成并核对本地结果，不能判明的交宿主处理。原生工具等待人工批准时保存 Runner 的 RunState，不提交终态，也不把原生工具审批混作 RSI 审批。

### 3.3 方式三：直接调用全部原子能力

```python
from cosens import CoSensClient

rsi = CoSensClient(base_url="http://27.221.10.54:41011", token="<ordinary-token>")
source = await rsi.atoms.artifact(name="input.txt", content="...")
prepared = await rsi.atoms.prepare(
    objective="...", input_artifact_ids=[source.artifact_id], key="stable-turn-key"
)
run = await rsi.atoms.get_run(prepared.run_id)
# 应用按 run.recipe / run.snapshot 自行执行任务。
await rsi.atoms.submit(run.run_id, data={"summary": "..."}, key="stable-submit-key")
```

完整原子方法与 HTTP 映射：

| SDK 方法 | 服务端接口 | 必要条件 |
| --- | --- | --- |
| `atoms.artifact` | `POST /v1/artifacts` | 文本材料；保存服务返回的 `artifact_id` 与 `content_hash` |
| `atoms.prepare` | `POST /v1/runs/prepare` | 稳定 `Idempotency-Key`；只准备，不执行 Agent |
| `atoms.get_run` | `GET /v1/runs/{run_id}` | 读取冻结的 Recipe/Memory 快照及哈希 |
| `atoms.session_events` | `POST /v1/runs/{run_id}/session-events` | 当前线上存在、附件精简版未列出；发送增量消息与执行事件，按 `durable_ack` 前进游标 |
| `atoms.submit` | `POST /v1/runs/{run_id}/submit` | 只记录真实终态结果；当前线上无通用幂等 Header，响应不明先 GET Run 对账 |
| `atoms.feedback` | `POST /v1/runs/{run_id}/feedback` | 只传有来源的真实反馈 |
| `atoms.improve` | `POST /v1/runs/{run_id}/improvements` | 非空原因；复用已有自动创建的 Job |
| `atoms.get_improvement` | `GET /v1/improvements/{job_id}` | 查询当前 Job 状态与报告 |
| `atoms.resume_improvement` | `POST /v1/improvements/{job_id}/resume` | 仅等待条件已补齐的 awaiting Job |
| `admin.atoms.publish` | `POST /internal/admin/v1/improvements/{job_id}/publish` | 独立管理员 token、validated、`expected_revision` 与原因 |

方式三不自动做模型触发判断、不自动运行 Agent、不自动发布。原子客户端可提供有界 `wait_improvement()` 辅助函数，但它只轮询已有 Job。`session_events` 是当前部署 OpenAPI 列出的第十个原子接口，供两种高层入口按消息游标上报；附件第 6 节的九接口清单反映另一版服务，不能用它推断该线上接口不存在。当前 OpenAPI 没把该接口的请求/响应字段展开，下文的事件格式和 ACK 规则来自仓库历史提交 `caaece4` 的同版服务源码与 `docs/RSI_WORKFLOW_API.md`，尚需用有权限的测试环境确认当前部署行为。

## 4. 三种模式的统一语义

| 入口与 `mode` | 决策与 CoSens 行为 | 原生执行 |
| --- | --- | --- |
| 透明调用，`auto` | prompt 判断是否激活；命中才上报 Session 增量并发起原子 RSI，未命中不发 CoSens 请求且游标不前进 | 照常执行一次 |
| 透明调用，`approval` | prompt 判断是否激活；命中后走服务端审批流程 | RSI 准备获批之前暂停 |
| 透明调用，`disable` | 不运行分类器、不提交 RSI 请求、不推进游标 | 原样执行一次 |
| 显式 `client.run(...)`，`disable` | 跳过 prompt 判断；显式调用本身强制执行原子流程，并增量上报 Session | 本地 Runner 执行一次 |

`mode` 控制隐式触发策略；显式 `run` 是更高优先级的启动指令。在显式 `run` 中，`mode="disable"` 表示“自动触发已关闭”，但用户明确调用 `run` 的意图仍要执行 RSI；这条优先级规则覆盖附件中“disable 不请求 RSI”的旧定义。`auto` 的模型判断是透明入口的前置门槛。若线上服务要求二次策略判断或审批，SDK 必须检查服务实际状态，不能凭本地决策越权。模型判断不是权限授权。

附件目标版的审批模式使用 `POST /v1/invocations`（`phase=prepare`、`mode=approval`），收到 `awaiting_approval` 后保存 `invocation_id`、`approval_id`、`context_hash`、`plan_hash`、`resume_token`，在原生执行之前抛 `RSIApprovalRequired`。管理员决定后，原调用方用同一 invocation 的 resume 继续，不重新分类或新建 Run。当前远端尚无这组接口，因此连接该地址时 `approval` 应在执行前报“服务能力不支持”，不能假装已审批；`auto` 收到远端要求审批的状态也要停止。若部署策略预先声明强制审批，分类失败不能退化成原生执行。

方式二只拼接原子接口，不调用 `/v1/invocations`；首版要求普通 `COSENS_RSI_TOKEN`。如果远端策略要求审批而拒绝原子 prepare/improve，就把服务响应交给调用方，并停止本次显式流程，不能私下切换到协调器。透明入口的 `auto` 命中也使用原子流程；`approval` 仍需要附件中的协调器。受限 `COSENS_RSI_CLIENT_TOKEN` 不能调用原子 `prepare/feedback/improve`，不能用于显式 `run`。管理员 token 只用于审批和发布，不放进普通包装 client。

附件第 5 节还规定四个审批协调接口；它们不属于显式 `run` 的原子编排，也**未出现在当前远端 OpenAPI 1.0.0 中**。透明入口 `approval` 要等远端升级到包含这些接口的契约后才能启用，不应调用当前远端的旧管理端审批 API 猜测等价行为：

| 协调器接口 | 用途 |
| --- | --- |
| `POST /v1/invocations` | 冻结 prepare/improve 阶段与 SessionContext，返回实际业务状态 |
| `GET /v1/invocations/{invocation_id}` | 查询准备、审批或改进状态；需要执行上下文时使用 `include_context=true` |
| `POST /v1/approvals/{approval_id}/decision` | 管理员批准或拒绝已冻结计划 |
| `POST /v1/invocations/{invocation_id}/resume` | 原请求主体消费批准，继续同一个 RSI 阶段 |

`GET /health/live` 与 `GET /health/ready` 只用于部署检查。Memory 的 `/memory/internal/v1/*` 是服务端内部协议，不是 SDK 对应用公开的蒸馏接口。

### 4.1 原生执行时每个远端接口的调用时机

以下 URL 均以 `http://27.221.10.54:41011` 为前缀。**显式 `run(mode="disable")` 从第 1 步直接开始；透明 `auto` 先做本地 prompt 判断，只有命中才进入第 1 步。**步骤按事件触发，没有后台定时上传全部历史。普通业务请求使用普通 RSI Bearer；实际参数与大小上限以附件第 8 节及当前远端版本为准。

| 顺序/时机 | 远端调用 | 发什么、保存什么 |
| --- | --- | --- |
| 0. 可选部署检查，非每次 run | `GET /health/live`、`GET /health/ready` | 检查服务进程/依赖；ready 需要 Bearer。不可用时在运行前报错 |
| 1. 冻结本次任务后，只有存在需作为原始材料的文本时 | `POST /v1/artifacts` | 上传真实输入材料；保存 `artifact_id/content_hash`。**普通聊天消息不逐条伪装为文件** |
| 2. 每个新 RSI turn 一次 | `POST /v1/runs/prepare` | `objective/task_family/input_artifact_ids/...`，稳定 `Idempotency-Key`；返回 `run_id/recipe/context`。`processing` 按服务 `retry_after_seconds`（附件示例 2 秒）同键重试，不新建 Run |
| 3. prepare 成功且需要完整冻结快照时 | `GET /v1/runs/{run_id}` | 读取 `snapshot/task_package_hash/snapshot_hash/recipe_revision`；据此给本次原生模型/Agent 注入有来源的 Recipe/Memory |
| 4. 原生模型/Agent 开始前 | `POST /v1/runs/{run_id}/session-events` | 只发送 Session 游标后的历史消息，1–200 条/批、每批最多 1 MB；逐条稳定 `id`；收到 `durable_ack` 后才前进游标。没有增量消息则不调用 |
| 5. 本地执行 | **无 RSI HTTP** | 第一种调用对应的原生 OpenAI 模型方法（例如 `responses.create` 或 `chat.completions.create`）；第二种调用原生 `Runner.run`。同一 turn 的工具循环不重复 prepare |
| 6. 原生新消息或工具结果实际产生后 | `POST /v1/runs/{run_id}/session-events` | 仅上报新增项，如 `agent_action/tool_result/session_end`；流式在事件完整且可持久化时分批上报，取消时不发完成事件。收到 ACK 再前进游标 |
| 7. 有文本交付物时 | `POST /v1/artifacts` | 上传实际输出文件并保存服务返回哈希；没有文件就不调用 |
| 8. 原生任务真正终止、结果已本地保存后 | `POST /v1/runs/{run_id}/submit` | `data` 包括真实 outcome/summary/输出 Artifact 引用；成功只表示结果记录，`evaluation=inconclusive`，不代表验收成功 |
| 9. 真实用户反馈到达时，可晚于 `run` 返回 | `POST /v1/runs/{run_id}/feedback` | 发送真实反馈，不从模型完成推断 accepted；若回执已有 improvement，复用其 `job_id` |
| 10. 有反馈或明确改进原因且没有现成 Job 时 | `POST /v1/runs/{run_id}/improvements` | 发送非空 `reason` 和稳定请求键，保存 `job_id`；没有学习依据时不调用 |
| 11. 已有 Job 且调用方要求观察进展时 | `GET /v1/improvements/{job_id}` | 有界轮询，不阻塞原生结果；建议从 2 秒退避到最多 30 秒，显式等待总时限默认 300 秒。以实时 Job 状态为准 |
| 12. Job 处于可恢复 awaiting 状态且缺口已补齐时 | `POST /v1/improvements/{job_id}/resume` | 只恢复原 Job，不重跑模型、不新建 Job；随后回到第 11 步 |
| 13. 有缺失的真实初始文件，且 Run 尚可补充时 | `POST /v1/runs/{run_id}/initial-inputs` | 当前远端有此接口；上传 Artifact 后提供 1–100 个 ID，不能拿最终答案冒充原始输入；冻结后必须新建 Run |
| 14. Job 已 validated 且管理员明确发布时 | `POST /internal/admin/v1/improvements/{job_id}/publish` | 独立管理员 token、`expected_revision` 和发布原因；不由 `client.run` 自动调用。新 Recipe 只进入后续 Run |

第 4、6 步是当前线上 `1.0.0` 的真实路径，附件目标 `2.1.0` 没有列出。其字段与回执来自同版旧源码，当前远端 OpenAPI 未详细声明；未经授权的写接口测试前，只能把它们作为待核验的实现契约。SDK 应在初始化时按远端能力选择受支持的版本；**如果线上升级后移除了 `session-events` 且没有等价入口，不能继续声称已完成 Session 消息上报**。本设计的显式 `run` 不使用 `/v1/trigger/decide`、`/v1/invocations` 或管理端审批入口来替代原子编排。

## 5. Session 消息增量上报与游标

游标由 SDK 持久化，键为 `(base_url, RSI 调用主体, session_id, session_incarnation)`，值至少有 `acked_index`、`acked_message_id`、该项内容哈希、会话来源版本、最近一次 `run_id` 和待确认事件批次。`session_incarnation` 用于区分同名 Session 被清空重建。不同 Session 独立并发；同一个 Session 的读取、上传、ACK 和游标提交必须串行，或以 compare-and-swap 防止旧请求倒退游标。

每次显式 `run`，或透明 `auto` 的 prompt 判断为“需要提交 RSI”后，SDK 读取当前 `session.get_items()`。只有索引大于 `acked_index` 的消息才形成待发批次；若宿主 Session 已有稳定消息 ID，使用它；否则用 `(session_id, session_incarnation, 原始索引, 规范化消息哈希)` 派生稳定 ID。必须验证游标之前的消息前缀仍与保存的 ID/哈希一致；若历史被截断、修改或重排，报 `SessionHistoryChanged`，要求宿主提供新 incarnation 或显式对账，不能悄悄从某个位置继续。

线上 `POST /v1/runs/{run_id}/session-events` 接受 `{ "events": [...] }`，每项为 `{ "id": string, "kind": string, "payload": object }`。映射建议：用户消息为 `user_prompt`，assistant 消息为 `agent_action`，工具结果为 `tool_result`，可授权的上下文传递为 `context_delivery`，真实纠错为 `correction`，终态为 `session_end`。`payload` 保留 `session_id/message_id/role/index/content/source_revision` 等有来源字段；敏感数据按应用配置裁剪。服务返回的 `durable_ack` 是每条事件的持久化确认，SDK 只把**连续获 ACK 的前缀**写入游标。部分 ACK 重发未确认项；请求或响应丢失时使用同一 Run、同一事件 ID、同一内容重试。线上服务按 `(run_id, event.id)` 去重，同 ID 变内容会拒绝。

```json
{
  "events": [{
    "id": "chat:<stable-message-id>",
    "kind": "user_prompt",
    "payload": {
      "session_id": "session-123",
      "message_id": "msg-8",
      "index": 8,
      "role": "user",
      "content": "上一轮用户消息",
      "source_revision": "session-rev-8"
    }
  }]
}
```

若透明 `auto` 连续几轮都不命中，游标保持原值；之后首次命中时，将这几轮中仍可从 Session 读取的未确认消息依序补发。SDK 不会因为某轮未触发而把它们标记为已发送。

游标跨 Run 生效：先前 Run 已 ACK 的旧消息不会在下次 Run 再上传，新的 Run 只收到新的消息。远端的事件去重仅在单个 Run 内，**跨 Run 不重发主要由 SDK 本地持久游标保证**；如果本地游标和恢复日志全部丢失，当前服务没有按 Session 全局查询/去重的接口，无法证明“绝不重发”。为保证宕机恢复，SDK 在发请求前保存 Run ID 与待发事件，收到 ACK 后以事务提交游标；重启先恢复未完成 Run，不先创建新 Run。若部署要求多实例共享游标或丢失本地状态后仍严格去重，需要服务端新增按 `(主体, session_id, message_id)` 的全局 ACK/查询能力。

`user_input` 若尚未写入 Session，不作为“既有历史消息”提前伪造发送；它已经在本次 `prepare.objective` 中体现。原生调用后从 Session 的真实新增项采集并发送；若宿主不自动把新输入写入 Session，则由适配器从本次原生输入/输出建立有来源的事件，写入同一游标序列。透明入口如果只给 `previous_response_id` 或 `conversation`，没有可读 Session，无法凭远端引用拉取全部旧消息；此时只上报当前调用可见项，标识 `history.coverage=partial`。

消息事件写入**不等于** Memory 已完成蒸馏，也不会自动发布 Recipe。线上回执只保证 `coverage=observable_events` 的可观测证据持久化；反馈、改进与独立验证仍按第 4.1 节走后续接口。现有接口按 Run 绑定事件，不能在新 Run 的 prepare 之前直接写入尚未归属的旧消息；因此本次 prepare 冻结的 Recipe/Memory 不保证已经利用刚上报的历史消息。原生 Agent 自己的 Session 仍按原方式提供历史。若产品要求“先蒸馏所有历史，再让同一次 prepare 召回这些新记忆”，后端还需会话级预摄取接口或等价能力，当前线上 API 无法保证。

附件的 `SessionContext` 只适用于目标版协调器流程；本版显式 `run` 不把全量历史打包进每次 `/v1/invocations`。未来启用透明 `approval` 时，目标版 Schema 还需要接受原始 Responses 来源，例如 `provider="openai_responses_sdk"`，且明确 Agent 字段是请求描述，不是完整 Agent 实例。当前远端既无协调器，也无该来源协议，不能宣称已支持审批入口。

## 6. 状态、恢复和错误

每个逻辑 turn 的本地状态至少含：决策结果（显式 `run` 可记为 `forced`）、冻结任务参数、`turn_id`、准备请求键、`run_id`、Session 游标、待发/已 ACK 事件 ID、原生执行状态、原生结果引用、提交请求键、反馈/Job 句柄和最后错误。存储方式由实现者选择，但必须能在同进程重试和重启后防止重复执行模型/工具；无法判定原生副作用是否发生时返回 `RSIRecoveryRequired` 交宿主对账。

当前原子流程区分 Run 的 `ready/completed`、事件的 `durable_ack`、Job 的 `queued/running/awaiting_*/validated/published/rejected/failed/deferred`。附件协调器的 `prepared/awaiting_approval/skipped` 属于另一个尚未部署的协议版本。`completed` 不等于验证通过；`validated` 需要管理员明确发布，且发布只影响后续新 Run。等待 Job 应有超时与退避，`resume_improvement` 仅用于指定 awaiting 状态。

HTTP 层保存状态码、领域错误码、detail 和请求 ID。线上 `1.0.0` 的 `Idempotency-Key` 只见于 `prepare` 和 `improvements`；附件 `2.1.0` 对 artifact/submit/feedback/publish 另有可选幂等键约定，不能提前套用到当前远端。`session-events` 依赖稳定事件 ID 与待核验的 `durable_ack`，不能假设其接受幂等 Header。线上版本的输出 Artifact 上传后若响应丢失，可能留下无法查询的孤儿 Artifact；submit 响应丢失时先 `GET Run` 读取状态与结果，已完成且匹配则记为成功，仍为 ready 才谨慎重试，不确定则进入 `RSIRecoveryRequired`。反馈和发布等无通用幂等保证的写请求也不得盲目重放。`prepare` 返回 `processing` 时用同一键轮询。回调或诊断写入失败不批准请求，也不重跑 Agent。

## 7. Python 模块划分与验收

后续实施时可按职责拆为：`factory`（类型分派）、`responses_adapter`、`agents_adapter`、`decision`、`session_cursor`、`session_events`、`atoms/http`、`approval`、`state`、`errors`。这些是设计上的模块边界，本轮不创建包目录或代码。

验收至少覆盖：

1. 对已支持的 `openai` 包版本，`OpenAI/AsyncOpenAI`（及传入时的 Azure 变体）的公开资源和方法均可经代理调用；覆盖嵌套资源、`with_options/copy`、raw/streaming response、分页、文件、Realtime、上下文管理、关闭、自定义请求和原生异常。透明 `disable` 的 CoSens 与分类器请求数均为零。
2. 透明 `auto` 的 prompt 判断否/是/失败三个分支；只有命中才增量上报并创建 Run，未命中不前进游标；工具续接不重复 prepare。
3. 逐一验证被标为支持 RSI 的模型生成入口：普通、raw、流式、同步与异步调用均保持原生返回语义；不具备安全归一化能力的入口保留原功能并暴露 `rsi_capture=unsupported`。
4. 显式 `run(mode="disable")` 不请求判断模型，仍执行输入 Artifact → prepare → GET Run → Session 增量事件 → 原生 Agent → 新增事件 → 输出 Artifact → submit → 真实 feedback/improve → GET Job；每个 RSI 调用都是 `atoms.*`。
5. 同一 Session 多次 `run` 仅发送新消息；部分 ACK、同 Run 重试、重启恢复不丢消息或重复执行模型；历史截断/改写时明确报错。
6. 当前远端 `1.0.0` 的 `session-events` 路径可用，而附件 `2.1.0` 的协调器不可用；`approval` 在能力缺失时明确报错；管理员发布单独授权。
7. 原生完成后 HTTP 失败先对账再补交；无真实反馈不生成 `accepted`；未验证 Job 不发布；`observable_events` 不被误写成“Memory 已蒸馏完成”。

## 8. TypeScript 预留

未来 TypeScript SDK 使用相同的 `mode`、`turn_id`、Session 消息 ID/游标、原子资源字段、幂等键和审批状态语义。根目录的 `typescript/` 目前只有预留说明；本轮不安装依赖、不编写 TypeScript 代码，也不承诺已有 TypeScript Agents SDK 的具体 API。跨语言协议以实际服务端 OpenAPI、附件契约与本设计中已确认的行为为准。

## 9. 待产品/后端确认的接口决策

1. 远端 `1.0.0` 与附件 `2.1.0` 的差异需要有明确的部署版本目标；当前地址有 `session-events`，没有 `/v1/invocations`。当前 OpenAPI 对前者使用开放 JSON/空响应 Schema，虽有同版旧源码说明 `events/durable_ack`，SDK 实施前仍需在授权测试环境核实。若未来升级删除该接口，必须提供等价的消息上报和 ACK 能力。
2. 若要求跨 Run、跨实例、丢失本地状态后仍绝对不重发消息，后端需要 Session 级全局消息 ID 去重和 ACK 查询；当前部署只对同一 Run 的事件去重。
3. 若要求“刚上传的历史消息立即影响本次 prepare 的 Memory 召回”，后端需要 prepare 前的会话级摄取；当前 `session-events` 只能在获得 run_id 后调用。
4. 透明 `approval` 需要部署附件协调器，并扩展 `openai_responses_sdk` 的 SessionContext 来源；当前地址不能执行该模式。

## 10. 官方参考

- [OpenAI Python Agents SDK 运行循环](https://openai.github.io/openai-agents-python/running_agents/)
- [OpenAI Python SDK 公开客户端及调用方式](https://github.com/openai/openai-python/blob/main/README.md)
- [OpenAI Responses API 的结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [OpenAI Responses API 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI Responses API 会话状态](https://developers.openai.com/api/docs/guides/conversation-state)
