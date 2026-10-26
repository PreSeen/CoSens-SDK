# CoSens Python SDK 三种接入方式设计

状态：设计稿，待确认；本文件不代表 SDK 已实现。

参考契约：[RSI_WORKFLOW_API(1).md](/Users/a1111/Desktop/RSI_WORKFLOW_API(1).md)，尤其是第 3、4.3、5、6、8 节。该文件描述的是已有 RSI 服务端接口；本仓库当前没有可复用的 SDK 源码。本设计以用户后续澄清为准，明确覆盖参考契约第 1.1、2、4.1、4.2 节中的旧 SDK 入口定义。

## 1. 目标与边界

CoSens 在用户原有的 OpenAI 调用旁收集被授权的对话与执行证据，调用 RSI 服务准备 Recipe/Memory、记录 Run 结果，并在获得真实反馈或明确改进原因后启动改进。Agent/工具仍在使用方进程中执行；RSI 服务只负责准备、记录、学习、验证与发布。

Python 首版提供三层入口：

1. **透明调用**：`cosens(OpenAI(), mode="auto")` 返回代理，拦截 `responses.create`。应用不显式调用 CoSens 的 `run`。`auto` 由模型判断该次任务是否需要提交 RSI Run。
2. **显式运行**：`cosens(agent, mode="auto").run(agent, user_input, session=...)`。SDK 持有原生 Agents SDK Runner，在一次 `run` 中编排参考契约第 4.3 节的完整原子流程。
3. **原子调用**：`CoSensClient(...).atoms.*` 直接暴露参考契约第 6 节全部九个原子操作，应用自行决定步骤与执行时机。

三个入口共用认证、传输、数据模型、幂等键、恢复记录与错误模型。TypeScript 只保留未来协议与目录规划，本轮不开发 TypeScript，也不实现任何 SDK 代码。

本设计不改变 RSI 算法、Memory 内部接口、模型工具执行权限、Agent 原始业务结果或服务端发布规则。`submit` 只记录结果，不能被解释为验收成功；`validated` 也不会自动发布。

## 2. 方案选择与关键取舍

| 候选方案 | 适用性与取舍 | 决定 |
| --- | --- | --- |
| 只包装 `agents.Runner` | 能准确看到一个 Agent turn、工具和 Session，但不满足第一种“封装原始 OpenAI SDK、拦截 `responses.create`”的要求 | 不作为第一种；用于第二种 |
| 只包装 `openai.OpenAI` | 改动少；一次 API 调用只能看到请求和响应，无法天然知道应用工具何时执行、整轮任务何时结束 | 作为第一种，明确能力边界 |
| 一个工厂按对象类型分派到两个代理，并共用原子客户端 | 同时满足透明接入和完整 Agent 编排；维护两套边界采集器 | 采用 |

`cosens()` 不修改传入对象，也不对 `openai` 或 `agents` 做全局 monkey patch。再次包装同一代理且配置一致时返回原代理；配置冲突时报错，避免一条响应被重复上报。

## 3. 公开 Python 用法

### 3.1 方式一：包装原始 OpenAI SDK

```python
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(native, mode="auto", decision_model="<configured-model>")

response = client.responses.create(model="<application-model>", input="用户任务")
print(response.output_text)
```

公开入口为同步 `OpenAI` 与异步 `AsyncOpenAI` 的 `responses.create`。原有参数和返回值保持 OpenAI SDK 语义；未拦截的方法委托给原客户端。代理额外提供 `client.cosens.get_turn(turn_id)` 查询 RSI 状态。只构造 `client` 不发送请求；只有调用被支持的 `responses.create` 才进入流程。

模型判断使用传给 `cosens()` 的 `decision_model`；启用 `auto/approval` 时它是必填配置，不猜测应用调用中的 `model` 是否适合分类。判断请求经**未包装的** OpenAI 客户端发出，要求结构化结果：`activate: bool`、`objective`、`task_family`、`reason`。这是一次额外模型调用，会增加费用和首次响应延迟。首版默认只给分类器当前请求中可见的最近用户文本与工具名称，截取上限 8,000 字符；不发送文件正文、工具输出、系统/开发者指令或完整历史。可以通过显式采集策略扩大范围，但必须由应用配置且仍受授权约束。建议默认决策超时 5 秒、输出上限 256 token，均可配置。原始调用中的 `store`、`metadata`、`tools` 等不被改写为分类器的工具权限。

当 `activate=false`，原生 `responses.create` 执行一次，不请求 CoSens，不创建 RSI Run。当 `activate=true`，SDK 冻结快照、提交 RSI 准备请求、在原有 `instructions/input` 上附加有来源的 Recipe/Memory 片段，再执行原生调用；不得删除或重排原请求中的指令、工具与内容。若请求形态无法安全注入，必须在原生调用前报明确错误，而不是悄悄省略已准备的 Recipe/Memory。分类失败、超时或无法解析时，`auto` **不向 CoSens 提交**、原生调用继续，并通过本地状态和可选事件回调暴露 `decision_failed`；`approval` 则在原生执行前报 `RSIDecisionUnavailable`，不能绕过可能要求的审批。`disable` 不调用分类器，也不请求 CoSens。

一条 `responses.create` 不等于完整 Agent turn。首版以“已收到最终 assistant 消息、没有待处理的 function call，且响应完整结束”为自动终态；该响应之后的用户新输入开始新 turn。若应用用 `previous_response_id` 或 `conversation` 续接模型与工具循环，代理用服务返回的响应 ID 和原生引用关联同一逻辑 turn。无法可靠关联时，不推断工具已执行或任务已完成；调用方可用可选 `client.cosens.scope(session_id=..., turn_id=...)` 显式给出边界。此 scope 不改变原生请求参数。仅有 function call、未见工具结果的响应保持 `awaiting_continuation`；若后续始终没有可关联调用，过期后标为 `incomplete`，不 submit。受限于原始 SDK 的可见性，应用在代理外执行的工具副作用、外部 Session 全量历史和人工审批，方式一不能保证自动捕获；需要完整轨迹时使用方式二。只有真正使用被包装 client 的请求会被拦截，其他 OpenAI client 不受影响。

流式调用保持原生事件次序。分类和准备发生在打开原生流之前；收到 `response.completed` 且对应逻辑 turn 已终结后才提交结果。流取消、断线或只有工具请求时不伪造完成。异步版本遵循相同规则；不得把同步调用暗中切换成异步调用。原生异常原样传播，已产生的 RSI 资源与恢复信息保留。

### 3.2 方式二：显式 `run` 包装 Agents SDK

```python
from agents import Agent
from cosens import cosens

agent = Agent(name="assistant", model="<application-model>", tools=tools)
client = cosens(agent, mode="auto", decision_model="<configured-model>")
result = await client.run(agent, user_input, session=session)
print(result.final_output)
```

`cosens(Agent)` 返回绑定该 Agent 的 `CoSensAgentRunner`；`run(agent, input, **native_options)` 是显式入口，并把 `session` 等原生选项交给 Agents SDK `Runner.run`。正常完成返回原生 `RunResult`，RSI 元数据通过 `client.cosens.get_turn(turn_id)` 获取。`run_sync`、`run_streamed` 按相同边界设计，不能伪造或替换原生工具调用、handoff、guardrail 与流事件。

每次顶层 `run` 只有一个 `turn_id` 和至多一个准备 Run；该 run 内的多轮模型调用、工具调用、handoff 与原生工具审批恢复都属于同一 turn。为本次调用克隆/组合有效指令并注入冻结的 Recipe/Memory，不改并发共享的 Agent 对象。分类器和原生 Runner 使用不同调用通道，避免分类器被方式一的代理再次拦截。

**一次正常命中的调用顺序**：冻结 `before_turn` → 模型决策 → 上传授权的原始文本材料 `POST /v1/artifacts` → `POST /v1/runs/prepare` → `GET /v1/runs/{run_id}` → 原生 `Runner.run` → 持久化最终结果 → 按需上传文本交付物 → `POST /v1/runs/{run_id}/submit` → 有真实反馈时 `POST /v1/runs/{run_id}/feedback` → 有真实反馈或明确改进原因时 `POST /v1/runs/{run_id}/improvements` → 需要观察时 `GET /v1/improvements/{job_id}`。管理员发布是独立动作，不属于 `client.run` 返回前的自动步骤。反馈接口可能已经自动创建 Job；此时复用返回的 Job，不再重复调用 improvements。

若 `auto` 决策为不激活，直接运行原生 Runner 一次。若 Agent 原生执行失败，保留原异常及 Run 状态，不假装已提交成功。若 Agent 完成后 CoSens 上传或提交失败，先保存原生结果和稳定请求键，返回原生结果并暴露 `completion_pending`；恢复时只补 CoSens HTTP，不重跑 Agent。原生工具等待人工批准时保存 Runner 的 RunState，不提交终态，也不把原生工具审批混作 RSI 审批。

### 3.3 方式三：直接调用全部原子能力

```python
from cosens import CoSensClient

rsi = CoSensClient(base_url="<rsi-service>", token="<ordinary-token>")
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
| `atoms.submit` | `POST /v1/runs/{run_id}/submit` | 只记录真实终态结果；建议稳定键 |
| `atoms.feedback` | `POST /v1/runs/{run_id}/feedback` | 只传有来源的真实反馈 |
| `atoms.improve` | `POST /v1/runs/{run_id}/improvements` | 非空原因；复用已有自动创建的 Job |
| `atoms.get_improvement` | `GET /v1/improvements/{job_id}` | 查询当前 Job 状态与报告 |
| `atoms.resume_improvement` | `POST /v1/improvements/{job_id}/resume` | 仅等待条件已补齐的 awaiting Job |
| `admin.atoms.publish` | `POST /internal/admin/v1/improvements/{job_id}/publish` | 独立管理员 token、validated、`expected_revision` 与原因 |

方式三不自动做模型触发判断、不自动运行 Agent、不自动发布。原子客户端可提供有界 `wait_improvement()` 辅助函数，但它只轮询已有 Job。

## 4. 三种模式的统一语义

| `mode` | 决策与 CoSens 行为 | 原生执行 |
| --- | --- | --- |
| `auto` | 模型判断是否激活；命中后发起 RSI，未命中无 CoSens 请求 | 照常执行一次 |
| `approval` | 模型判断是否激活；命中后走服务端协调器的审批与恢复流程 | RSI 准备获批之前暂停 |
| `disable` | 不运行分类器、不提交 RSI 请求、不注入 Recipe/Memory | 原样执行一次 |

`auto` 的模型判断是 SDK 前置门槛。RSI 服务仍可根据自身策略拒绝、跳过或强制审批，所以 SDK 必须检查服务的实际状态。此设计覆盖附件中“统一由服务端 auto 分类”的旧叙述；服务端若仍要求二次分类，可能出现模型判定命中但服务返回 `skipped`，SDK 按实际响应执行原生调用且不 submit。模型判断不是权限授权，`approval` 也不能用原子 prepare 代替审批。

审批模式使用 `POST /v1/invocations`（`phase=prepare`、`mode=approval`），收到 `awaiting_approval` 后保存 `invocation_id`、`approval_id`、`context_hash`、`plan_hash`、`resume_token`，在原生执行之前抛 `RSIApprovalRequired`。管理员 `POST /v1/approvals/{approval_id}/decision` 后，原调用方用 `POST /v1/invocations/{invocation_id}/resume` 继续同一个请求，不再重新分类或新建 Run。结果后的学习审批同理使用 `phase=improve`；原生 Agent 不重跑。服务配置或受限 token 强制审批时，即使调用方请求 `auto` 也应按服务返回的状态暂停。若部署策略预先声明强制审批，分类失败也必须按 `approval` 的失败规则处理，不能退化成原生执行。

因此，方式二在 `auto` 正常命中时编排原子接口；在 `approval` 或服务强制审批时，准备/改进阶段通过协调器，获批后继续原子后半段。方式一也依此规则。受限 `COSENS_RSI_CLIENT_TOKEN` 不能调用原子 `prepare/feedback/improve`，若配置该凭证，自动模式的准备/学习也走协调器；普通 `COSENS_RSI_TOKEN` 用于原子流程。管理员 token 只用于审批和发布，不放进普通包装 client。

审批与状态恢复另外使用附件第 5 节的四个协调接口；它们不属于第 6 节的九个原子接口：

| 协调器接口 | 用途 |
| --- | --- |
| `POST /v1/invocations` | 冻结 prepare/improve 阶段与 SessionContext，返回实际业务状态 |
| `GET /v1/invocations/{invocation_id}` | 查询准备、审批或改进状态；需要执行上下文时使用 `include_context=true` |
| `POST /v1/approvals/{approval_id}/decision` | 管理员批准或拒绝已冻结计划 |
| `POST /v1/invocations/{invocation_id}/resume` | 原请求主体消费批准，继续同一个 RSI 阶段 |

`GET /health/live` 与 `GET /health/ready` 只用于部署检查。Memory 的 `/memory/internal/v1/*` 是服务端内部协议，不是 SDK 对应用公开的蒸馏接口。

## 5. 数据契约、蒸馏边界与后端缺口

两种高层入口把已获授权、实际可见的输入和历史转成附件第 3 节的 `SessionContext`：稳定 `session_id/turn_id`、`before_turn/after_turn`、任务目标、输入、历史覆盖度、Agent 配置、文本材料、结果、真实反馈，以及来自 Run 的 `run_id/task_package_hash/snapshot_hash`。请求重试必须复用同一冻结正文、`captured_at` 和幂等键；不能重新抓取改变后的历史再沿用旧键。敏感字段、函数对象、凭证、客户端实例和未授权历史不进入快照。附件规定的总快照 2 MB、输入/历史项数、文本产物大小和结果大小在 SDK 发送前校验。

附件的 `SessionContext.provider` 首版固定为 `openai_agents_sdk`，原始 `OpenAI.responses.create` 没有 Agent 对象，因此方式一还需要后端扩展 Schema：允许 `provider="openai_responses_sdk"`，`agent.name="openai-responses-client"`，`agent.model` 取本次 `model` 参数，`agent.instructions` 取本次显式指令，`agent.tools` 只包含请求中的工具声明，`agent.config_hash` 覆盖这些可见配置与响应格式。`history.strategy` 依据 `conversation`/`previous_response_id` 或显式输入选择，未知历史标 `partial`。这组字段是**模型请求描述**，不能冒充应用 Agent 的完整工具执行配置。后端协调器审批、观察接口和验证逻辑都必须接受并保留该来源类型；完成这项 Schema 扩展前，方式一不能宣称支持完整 RSI 准备或审批。

原子 `prepare/submit` 可完成 Recipe/Memory 冻结与结果记录，但**现有九个公开原子接口没有独立的 SessionContext 事件写入接口**。因此方式二仅走原子接口时，不能声称已经把完整对话、工具轨迹蒸馏进服务端 Memory。为满足完整目标，建议后端新增 `POST /v1/runs/{run_id}/observations`。这是本设计提出的**新增接口**，附件中尚不存在，开发 SDK 完整蒸馏能力前须由后端实现并确认：

| 字段 | 建议契约 |
| --- | --- |
| 认证 | 普通 RSI token；受限 token 仅可写自身已批准的 Run；管理员 token 不代替业务主体 |
| Header | `Idempotency-Key` 必填；同键同正文返回原回执，同键变参返回 409 |
| Body | `{ "session_context": SessionContext, "stage": "before_turn" | "after_turn" | "interrupted", "observation_key": string }`；`session_context.rsi_binding.run_id` 必须等于 URL 的 `run_id`，哈希须与冻结 Run 一致；多次 interrupted 使用不同稳定键 |
| 回执 | `{ "observation_id": string, "run_id": string, "turn_id": string, "stage": string, "status": "recorded" }` |
| 服务行为 | 校验来源/主体/大小限制，按 `run_id + observation_key` 去重，记录有来源的对话与工具事件；不自动重跑 Agent、不推断 accepted、不直接发布 Recipe |

调用顺序是在 `GET Run` 后把准备前冻结的 `before_turn` 快照补上服务返回的 `rsi_binding`、重算 `context_hash`，再写入观察；原生执行及 `submit` 后写 `after_turn` 观察，随后才由真实反馈触发 `feedback/improve`。流式工具续接可按同一 `turn_id` 写 `interrupted`，但不得当作最终结果。服务端的蒸馏作业由该观察与后续反馈/验证材料驱动，其内部 Memory API 仍不暴露给应用。若后端尚未实现此接口，SDK 首版只按现有原子能力提交任务输入、文本 Artifact、结果与反馈，并对外标识 `session_distillation=unavailable`；不能把 `/memory/internal/v1/*` 直接暴露给应用或暗称完整聊天蒸馏已生效。

原始 `responses.create` 只暴露该 API 调用中的内容。若使用 `previous_response_id` 或 `conversation` 且无法导出历史，`history.coverage=partial`，不能声称服务端已收到完整聊天内容。原生 `Runner.run` 可以通过 Session 和 `new_items` 获取更完整的 turn，但也只能导出应用授权可读的内容。模型判断为“不激活”的 turn 不向 CoSens 发送内容，这是 `auto` 的明确语义；若产品要求每条消息都蒸馏，应增加单独的采集策略，而不是悄悄改变 `auto`。

## 6. 状态、恢复和错误

每个逻辑 turn 的本地状态至少含：决策结果、冻结快照、`turn_id`、准备请求键、`run_id`、原生执行状态、原生结果引用、提交请求键、反馈/Job 句柄和最后错误。存储方式由实现者选择，但必须能在同进程重试和重启后防止重复执行模型/工具；无法判定原生副作用是否发生时返回 `RSIRecoveryRequired` 交宿主对账。

高层入口区分三类状态：RSI invocation 的 `prepared/awaiting_approval/skipped`、Run 的 `ready/completed`、Job 的 `queued/running/awaiting_*/validated/published/rejected/failed/deferred`。`completed` 不等于验证通过；`validated` 需要管理员明确发布，且发布只影响后续新 Run。等待 Job 应有超时与退避，`resume_improvement` 仅用于指定 awaiting 状态。

HTTP 层保存状态码、领域错误码、detail 和请求 ID。`prepare` 与协调器写入必须用 `Idempotency-Key`；artifact/submit/feedback/publish 即使服务允许省略，SDK 也默认使用稳定键。相同键重试原请求，不能换键制造新 Run。`prepare` 返回 `processing` 时用同一键轮询；响应丢失时同键重放。回调或诊断写入失败不批准请求，也不重跑 Agent。

## 7. Python 模块划分与验收

后续实施时可按职责拆为：`factory`（类型分派）、`responses_adapter`、`agents_adapter`、`decision`、`session_context`、`atoms/http`、`approval`、`state`、`errors`。这些是设计上的模块边界，本轮不创建包目录或代码。

验收至少覆盖：

1. `OpenAI` 与 `AsyncOpenAI` 的 `responses.create` 同步/异步、普通/流式行为；`disable` 的 CoSens 与分类器请求数均为零。
2. `auto` 模型判断否/是/失败的三个分支；命中只创建一个逻辑 Run，工具续接不重复准备。
3. 显式 `run` 的输入 Artifact → prepare → GET Run → 原生 Agent → 输出 Artifact → submit → 真实 feedback/improve → GET Job 顺序；原生结果、工具与 Session 语义保留。
4. 准备审批与学习审批分别暂停和恢复；受限凭证不能直接调用原子 prepare/improve；管理员发布单独授权。
5. 原生完成后 HTTP 失败只补交结果；同键重放、并发 turn、流取消和重启恢复均不重复调用模型/工具。
6. 不完整历史标记 `partial`；无真实反馈不生成 `accepted`；未验证 Job 不发布；完整聊天蒸馏的后端协议缺口被显式检测。

## 8. TypeScript 预留

未来 TypeScript SDK 使用相同的 `mode`、`turn_id`、SessionContext、原子资源字段、幂等键和审批状态语义。可规划 `sdk/typescript/` 与独立的 Responses/Agent 适配器，但本轮不创建目录、不安装依赖、不承诺已有 TypeScript Agents SDK 的具体 API。跨语言协议以服务端 OpenAPI、附件契约与本设计中已确认的行为为准。

## 9. 待产品/后端确认的接口决策

1. 确认并实现第 5 节提出的观察接口，或提供等价的协调器入口；完整会话与工具事件蒸馏的交付以此为前置条件。在此之前可单独验收现有原子能力闭环。
2. 确认并实现 `openai_responses_sdk` 来源的 SessionContext Schema 和协调器处理；第一种透明入口及其审批依赖此扩展。
3. 复核本设计选定的方式一终态规则与决策数据预算；若产品希望跨多个最终 assistant 消息合并成一个业务任务，需要由宿主显式提供 scope/结束信号。

## 10. 官方参考

- [OpenAI Python Agents SDK 运行循环](https://openai.github.io/openai-agents-python/running_agents/)
- [OpenAI Responses API 的结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [OpenAI Responses API 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI Responses API 会话状态](https://developers.openai.com/api/docs/guides/conversation-state)
