# CoSens SDK 三种接入方式设计与 TypeScript 复刻规范

> 2026-10-28 新增任务工作流契约：见 [Python / TypeScript 任务工作流 SDK 开发契约](2026-10-28-task-workflow-sdk-design.md)。本文其余内容描述旧 `/v1/runs` 协议；两套协议须按各自的入口和状态语义使用。

状态（2026-10-27）：Python SDK 已接入国内网关；根据 FULL_CURL_TRANSCRIPT.md 与线上 OpenAPI 完成公开 Memory/Recipe 独立召回和新增原子能力，真实网关联调见 `2026-10-27-gateway-validation.md`。本文件第 1–7 节说明 Python 现状与产品语义，第 8 节给出 TypeScript 复刻规范；`typescript/` 已有开发中的源码和模拟测试，尚未完成第 8.6 节的全部验收项。Python 工作区的 `_config.py` 已加入 `cn/us` 内部区域表，`us` 地址尚无本文件所述的网关联调证据。审批协调器仍不在已验证范围内。

参考契约：[RSI_WORKFLOW_API(1).md](/Users/a1111/Desktop/RSI_WORKFLOW_API(1).md)，尤其是第 3、4.3、5、6、8 节。本设计以用户后续澄清为准，覆盖参考契约第 1.1、2、4.1、4.2 节中的旧 SDK 入口定义。2026-10-26 对旧直连地址的只读查询中，`/openapi.json` 标为 `1.0.0`，与附件 2.1.0 不一致；2026-10-27 的 CN 网关联调另见验证报告。下文区分目标流程、已验证网关能力与仍未核验的能力。

## 1. 目标与边界

CoSens 在用户原有的 OpenAI 调用旁收集被授权的对话与执行证据，调用 RSI 服务准备 Recipe/Memory、记录 Run 结果，并在获得真实反馈或明确改进原因后启动改进。Agent/工具仍在使用方进程中执行；RSI 服务只负责准备、记录、学习、验证与发布。

Python 首版提供三层入口：

1. **透明调用**：`cosens(OpenAI(), mode="auto")` 返回覆盖原始 OpenAI client **全部公开功能**的兼容代理；每个业务 turn 都在模型调用前召回 Recipe/Memory，应用不显式调用 CoSens 的 `run`。`auto` 由模型判断是否自动提交 RSI Run；`disable` 关闭这一自动提交，不关闭召回。
2. **显式运行**：`cosens(agent, mode="disable").run(agent, user_input, session=...)`。`disable` 在这个入口关闭隐式触发判断；显式 `run` 本身是启动 RSI 的指令。SDK 在这一次 `run` 中直接拼接参考契约第 4.3 节的原子接口，不调用协调器。
3. **原子调用**：`CoSensClient(...).atoms.*` 暴露 Run、Recipe、Memory 和评估相关原子操作，包括已在 CN 网关验证的 `session_events`；应用自行决定步骤与执行时机。

三个入口共用认证、传输、数据模型、幂等键、Session 消息游标、恢复记录与错误模型。项目根目录按语言划分为 `python/` 和 `typescript/`；TypeScript 实现仍在开发中。

三个入口均不向应用暴露任意 CoSens 服务的 `base_url` 参数。内部 `COSENS_REGION` 允许列表选择服务，默认 CN 前缀为 `https://friday-rsi-cn.stardust-ai.com/api`；例如 `POST /v1/runs/prepare` 发往 `https://friday-rsi-cn.stardust-ai.com/api/v1/runs/prepare`。工作区也已配置 US 前缀 `https://rsi.cosens.ai/api`，但本文件的 2026-10-27 真实联调只覆盖 CN。网关转发 `/api` 后面的路径，不额外添加或删除 `/v1`、`/internal`、`/health`。原生 OpenAI client 的 `base_url` 仍只控制模型服务，与 CoSens 网关互不影响。

区域扩展（2026-10-27）：`COSENS_REGION=cn` 默认保持以上国内前缀；`us` 使用 `https://rsi.cosens.ai/api`，所有原子路径不变。客户端初始化时固定区域；Turn 状态和消息 ACK 游标使用对应网关身份隔离，保留国内既有命名空间。三种调用方式均适用，不新增公开 `base_url` 参数。美国区目前仅完成自动化模拟测试与连通性检查，真实端到端联调等待有效美国区 Key。

鉴权使用用户指定的请求头契约：初始化传 `api_key`（只填 Friday 用户 Key 本身），所有 CoSens 网关业务请求同时携带 `X-Auth-Type: api_key` 与 `Authorization: Bearer <api_key>`。此前的 [网关鉴权指南](/Users/a1111/IdeaProjects/friday-rsi/docs/api/gateway-authentication.md) 作为背景参考；本段更新优先于其中可能不同的旧请求示例。可从 `RSI_API_KEY` 读取，兼容 `COSENS_API_KEY`；不再接收旧 `token/admin_token` 参数，也不读取后端服务令牌环境变量。后端服务令牌由网关注入，SDK 不调用内省接口，也不接受或发送 `X-User-Id`/`X_User_Id`；网关从鉴权结果 accountId 自动注入。发布仍为显式管理员动作，使用用户 Key 经网关转发；当前网关文档没有承诺 RSI scope 或按账号隔离远端数据，SDK 不假定这两项已实现。

密钥传输使用 HTTPS + Bearer，按 [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750#section-5.3) 由 TLS 加密请求头和正文，并保持证书校验开启。网关传输仅接受服务相对路径，拒绝绝对/外部 URL，不跟随重定向；Key 不放 URL、业务 JSON、日志或持久化状态。应用从环境变量或部署密钥管理服务读取 Key，不在源码硬编码；不自行对 Key 做 Base64、哈希或自定义加密，也不改变网关的认证协议。

三个入口的共同要求是：每个业务 turn 首次业务模型调用前取得 Recipe/Memory，并在工具循环中复用。创建 Run 时使用 `prepare` 的 `recipe/context` 加 `get_run` 的 `snapshot`；不创建 Run 时组合 `GET /v1/recipes/active?task_family=...` 和 `POST /v1/memory/recall`。这是两次公开读取后冻结的本地上下文，不声称服务端跨接口事务一致性。合法 `not_found`/空列表可继续，失败或畸形响应必须阻止业务模型。

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
import os
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(
    native,
    api_key=os.environ["RSI_API_KEY"],
    mode="auto",
)

response = client.responses.create(model="<application-model>", input="用户任务")
print(response.output_text)
```

公开入口至少接收 `OpenAI` 与 `AsyncOpenAI` 客户端；若传入 `AzureOpenAI/AsyncAzureOpenAI`，也必须保留其原有资源、认证与 API 版本语义。代理额外提供 `client.cosens.get_turn(turn_id)` 查询 RSI 状态。只构造 `client` 不发送请求。若应用持有可读取的 Session，显式交给 `cosens(..., session=session)` 或本次 scope；原始 `OpenAI` 客户端自身不提供 `agents.Session`，SDK 无法凭 client 推断之前的聊天消息。

**完整 SDK 兼容契约**：`cosens(client)` 返回的对象必须让原 client 的所有公开资源树、方法、属性和调用链继续可用，而不能只实现一个 `responses.create` 门面。包括 `responses`、`chat`、`completions`、`files`、`audio`、`images`、`embeddings`、`batches`、`models`、`moderations`、`vector_stores`、`fine_tuning`、`realtime`、`beta`，以及当前所安装 OpenAI SDK 版本新增的公开资源。正常委托调用须保留位置/关键字参数、默认值、返回模型、异常类型、请求选项、重试、分页、文件与二进制传输、同步/异步和流式生命周期。`client.with_options(...)`/`copy(...)` 派生出的客户端仍保持 CoSens 包装及同一 Session 游标；`client.close()`、上下文管理器、异步上下文管理器按原生客户端语义释放资源。`with_raw_response`、`with_streaming_response` 及资源下的同名调用链也必须可达，不能因代理少了一层属性而失败。通用 `client.get/post/...` 自定义请求同样透传。

实现上使用递归资源代理与显式的模型请求适配器；SDK 每次按原生资源对象动态委托，不能手写一个随 OpenAI SDK 更新而过期的固定方法清单。对需要 CoSens 的模型生成调用，在请求发出前和结果实际完成后挂钩；非对话资源仍原样调用，不凭文件上传、模型列表或管理操作创建 RSI Run。`responses.create`、`responses.parse/stream`、`chat.completions.create/parse/stream`、旧式 `completions.create` 及其 raw/streaming 变体属于需要逐一验证的生成调用入口。对输入或输出无法安全归一化的生成变体，调用保持原生可用，并明确记录 `rsi_capture=unsupported`，不能静默宣称已经上报。批量任务和 Realtime 长连接同样要保持原功能；它们的 RSI 边界需有独立适配规则，不能把建立连接或创建 Batch 当作一次完成的聊天 Run。

兼容目标以 SDK 明确支持的 `openai` Python 包版本为单位验收：记录该版本的公开资源/方法清单，逐项验证代理存在、签名可调用、原生返回和错误可传递；升级 `openai` 包时重复此检查。动态委托可让新增方法仍可调用，但未经适配与验收的新模型生成方法不得被标为“已完成 RSI 采集”。CoSens 的方法仅放在 `client.cosens` 命名空间，避免覆盖原生属性。

代理的“功能可用”不等于 Python 名义类型完全相同：包装现有实例后，`isinstance(wrapped, OpenAI)` 等身份检查可能不同。对此保留 `client.native` 访问原对象；任何通过 `native` 发起的调用都不会被 CoSens 拦截。若第三方库强制接收原始类实例，需要单独的兼容适配，不得宣称完全透明。

基础入口只需 `native`、`api_key`、`mode`。启用 `auto/approval` 时，决策默认使用本次原生请求的 `model`，`decision_model` 仅为高级可选覆盖项。两者都未提供时在网络请求前返回明确配置错误。`session` 同样可选：不传时捕获当前调用可见的用户文本、模型回复和已适配工具事件，不能推断应用未提供的外部历史。判断请求经**未包装的** OpenAI 客户端的 Responses API 发出，要求该模型/服务支持结构化结果：`activate: bool`、`objective`、`task_family`、`reason`；不支持时按分类失败处理。这是一次额外模型调用，会增加费用和首次响应延迟。首版默认只给分类器当前请求中最后一条可见用户消息的文本与工具名称，截取上限 8,000 字符；不发送文件正文、工具输出、系统/开发者指令或完整历史。可以通过显式采集策略扩大范围，但必须由应用配置且仍受授权约束。建议默认决策超时 5 秒、输出上限 256 token，均可配置。原始调用中的 `store`、`metadata`、`tools` 等不被改写为分类器的工具权限。

透明入口每个新 turn 均须在首次业务 `responses.create` 等模型请求之前取得 Recipe/Memory。`mode="disable"` 不调用分类器、不自动创建或提交 Run，但仍组合公开 Recipe 与 Memory 接口召回。`auto` 由分类器先判断是否自动提交 Run；当 `activate=false` 时仍独立召回，然后原生请求执行一次，不创建 RSI Run，消息游标不前进；当 `activate=true` 时读取 Session 中**尚未收到远端确认**的历史消息，以一次 `atoms.prepare` 和 `atoms.get_run` 取得本 turn 冻结的 Recipe/Memory，再增量上报，已确认的历史消息不再上传。两条路径都在原有 `instructions/input` 上附加有来源的片段，不删除或重排原指令、工具与内容。召回失败或请求形态无法安全注入时，必须在业务模型调用前报明确错误。分类失败、超时或无法解析时，`auto` 不自动提交 Run、游标不前进，但仍独立召回后执行原生请求，并通过本地状态和可选事件回调暴露 `decision_failed`；`approval` 则在原生执行前报 `RSIDecisionUnavailable`。这里的分类器属于前置决策模型；召回前置约束针对业务模型调用。

一条 `responses.create` 不等于完整 Agent turn。首版以“已收到最终 assistant 消息、没有待处理的 function call，且响应完整结束”为自动终态；该响应之后的用户新输入开始新 turn。Responses 通过 `previous_response_id` 或 `conversation` 的原生引用续接；Chat Completions 则保存 `choices[].message.tool_calls[].id`，用后续 `messages` 中 `role=tool` 的 `tool_call_id` 续接同一逻辑 turn。工具调用 ID 需按 Session ID、incarnation 和调用主体隔离；多个工具结果指向不同 turn 时停止并要求显式恢复。关联到已有 turn 时，后续模型请求**复用已取得的 Recipe/Memory**，不重复独立召回或 prepare/get_run；有 Run 时工具调用及工具结果分别上报 `agent_action`、`tool_result`，工具结果须在下一次业务模型请求前取得 ACK。新用户输入形成新 turn 时才重新召回。无法可靠关联时，不推断工具已执行或任务已完成；调用方可用可选 `client.cosens.scope(session_id=..., turn_id=...)` 显式给出边界。此 scope 不改变原生请求参数。仅有 function call、未见工具结果的响应保持 `awaiting_continuation`；若后续始终没有可关联调用，过期后标为 `incomplete`，不 submit。受限于原始 SDK 的可见性，应用在代理外执行的工具副作用、外部 Session 全量历史和人工审批，方式一不能保证自动捕获；需要完整轨迹时使用方式二。只有真正使用被包装 client 的请求会被拦截，其他 OpenAI client 不受影响。

所有原生流式调用保持事件次序和迭代/上下文管理器语义。对 `responses.create(stream=True)`，本 turn 的召回在打开原生流之前完成；仅 `auto/approval` 运行触发分类，且仅需要创建 Run 的分支运行相应准备流程。收到 `response.completed` 且对应逻辑 turn 已终结后，才对已创建的 Run 提交结果。其他生成流采用各自的真实完成事件，不拿首个 chunk 当终态。流取消、断线或只有工具请求时不伪造完成。异步版本遵循相同规则；不得把同步调用暗中切换成异步调用。原生异常原样传播，已产生的 RSI 资源与恢复信息保留。

### 3.2 方式二：显式 `run` 包装 Agents SDK

```python
from agents import Agent
from cosens import cosens

agent = Agent(name="assistant", model="<application-model>", tools=tools)
client = cosens(agent, api_key="<Friday API Key>", mode="disable")
result = await client.run(agent, user_input, session=session)
print(result.final_output)
```

`cosens(Agent)` 返回绑定该 Agent 的 `CoSensAgentRunner`；`run(agent, input, **native_options)` 是显式入口，并把 `session` 等原生选项交给 Agents SDK `Runner.run`。`mode="disable"` 只关闭**隐式的 prompt 触发**，不取消用户已经显式调用的 CoSens `run`。此入口必定尝试 RSI 原子流程，不经过决策模型。正常完成返回原生 `RunResult`，RSI 元数据通过 `client.cosens.get_turn(turn_id)` 获取。`run_sync`、`run_streamed` 按相同边界设计，不能伪造或替换原生工具调用、handoff、guardrail 与流事件。

每次顶层 `run` 只有一个 `turn_id` 和至多一个准备 Run；该 run 内的多轮模型调用、工具调用、handoff 与原生工具审批恢复都属于同一 turn。`run` 必须先通过内部原子 `prepare/get_run` 召回并冻结 Recipe/Memory，随后才启动 `Runner.run` 的首次业务模型调用；后续工具循环复用快照。为本次调用克隆/组合有效指令并注入冻结的 Recipe/Memory，不改并发共享的 Agent 对象。进入原子流程前读取 `session.get_items()`（或宿主授权的等价读取接口），按第 5 节游标筛选增量消息。Session 中之前已经上报并确认的消息不再发；本次 `user_input` 若还未写入 Session，则作为本次任务目标使用，原生运行后再根据 Session 的实际新增项上报，不能靠同文本猜测它已经存在。

**一次显式调用的顺序**：读取 Session 与本地游标 → 上传本次任务需要的原始文本材料 `POST /v1/artifacts` → `POST /v1/runs/prepare` → `GET /v1/runs/{run_id}` → 把未确认的历史消息发送到 `POST /v1/runs/{run_id}/session-events` 并按 ACK 前进游标 → 原生 `Runner.run` → 持久化最终结果 → 把新增消息/工具结果通过同一事件接口增量上报 → 按需上传文本交付物 → `POST /v1/runs/{run_id}/submit` → 有真实反馈时 `POST /v1/runs/{run_id}/feedback` → 有真实反馈或明确改进原因时 `POST /v1/runs/{run_id}/improvements` → 需要观察时 `GET /v1/improvements/{job_id}`。所有 CoSens 请求由第三种的原子客户端发出；`client.run` 只是调用它们的本地编排器。管理员发布是独立动作，不属于 `client.run` 返回前的自动步骤。反馈接口可能已经自动创建 Job；此时复用返回的 Job，不再重复调用 improvements。

若增量消息发送失败，显式 `run` 在原生 Runner 启动前停止，并复用同一 Run 与事件 ID 重试；不推进游标。若 Agent 原生执行失败，保留原异常及 Run 状态，不假装已提交成功。若 Agent 完成后 CoSens 上报或提交失败，先保存原生结果并暴露 `completion_pending`；恢复时只对账和补交远端状态，不重跑 Agent。线上 `1.0.0` 的 artifact/submit/feedback 没有通用幂等 Header，响应结果不明时不能盲目重放：先 `GET Run` 检查是否已完成并核对本地结果，不能判明的交宿主处理。原生工具等待人工批准时保存 Runner 的 RunState，不提交终态，也不把原生工具审批混作 RSI 审批。

### 3.3 方式三：直接调用全部原子能力

```python
from cosens import CoSensClient

rsi = CoSensClient(api_key="<Friday API Key>")
source = await rsi.atoms.artifact(name="input.txt", content="...")
prepared = await rsi.atoms.prepare(
    objective="...", input_artifact_ids=[source.artifact_id], key="stable-turn-key"
)
run = await rsi.atoms.get_run(prepared.run_id)
# application_agent 是应用自己的模型执行入口；同一 turn 的工具循环复用此快照。
output = await application_agent(recipe=prepared.recipe, snapshot=run.snapshot)
await rsi.atoms.submit(run.run_id, data={"summary": output}, key="stable-submit-key")
```

完整原子方法与 HTTP 映射：

| SDK 方法 | 服务端接口 | 必要条件 |
| --- | --- | --- |
| `atoms.recall_context` | `GET /v1/recipes/active` + `POST /v1/memory/recall` | 不创建 Run；读取后冻结 Recipe 和 Memory，保留来源/版本；已实测 |
| `atoms.artifact` | `POST /v1/artifacts` | 文本材料；保存服务返回的 `artifact_id` 与 `content_hash` |
| `atoms.prepare` | `POST /v1/runs/prepare` | 稳定 `Idempotency-Key`；只准备，不执行 Agent |
| `atoms.get_run` | `GET /v1/runs/{run_id}` | 读取冻结的 Recipe/Memory 快照及哈希 |
| `atoms.session_events` | `POST /v1/runs/{run_id}/session-events` | CN 网关已实测；发送增量消息与执行事件，按 `durable_ack` 前进游标 |
| `atoms.submit` | `POST /v1/runs/{run_id}/submit` | 只记录真实终态结果；旧直连部署无通用幂等 Header，响应不明先 GET Run 对账 |
| `atoms.feedback` | `POST /v1/runs/{run_id}/feedback` | 只传有来源的真实反馈 |
| `atoms.improve` | `POST /v1/runs/{run_id}/improvements` | 非空原因；复用已有自动创建的 Job |
| `atoms.get_improvement` | `GET /v1/improvements/{job_id}` | 查询当前 Job 状态与报告 |
| `atoms.resume_improvement` | `POST /v1/improvements/{job_id}/resume` | 仅等待条件已补齐的 awaiting Job |
| `admin.atoms.publish` | `POST /internal/admin/v1/improvements/{job_id}/publish` | 显式管理员操作、Friday 用户 Key、validated、`expected_revision` 与原因 |

方式三不自动做模型触发判断、不运行 Agent、不自动发布。调用方在首次业务模型前执行 `prepare/get_run` 或 `recall_context(query=..., task_family="general", token_budget=1000)`，工具循环复用结果。`recall_context` 已组合两个公开接口，不创建 Run。`session_events` 的 `events` 请求体、`durable_ack`、重复事件 ACK 已在网关实测；完整路径和复现记录见联调报告。

## 4. 三种模式的统一语义

| 入口与 `mode` | 决策与 CoSens 行为 | 原生执行 |
| --- | --- | --- |
| 透明调用，`auto` | 每个新 turn 均召回；prompt 判断是否自动提交 Run，命中才上报 Session 增量并发起原子 RSI，未命中只召回且游标不前进 | 注入召回内容后执行一次 |
| 透明调用，`approval` | 每个新 turn 均须召回；命中需提交 Run 时走服务端审批流程 | RSI 准备获批之前暂停，获批后注入上下文执行 |
| 透明调用，`disable` | 每个新 turn 均独立召回；不运行分类器、不自动创建/提交 Run、不推进上报游标 | 注入召回内容后执行一次 |
| 显式 `client.run(...)`，`disable` | 跳过 prompt 判断；显式调用本身强制执行原子流程，并增量上报 Session | 本地 Runner 执行一次 |

`mode` **仅控制是否自动判断、创建和上报 RSI Run，不控制 Recipe/Memory 召回**；显式 `run` 是更高优先级的启动指令。在显式 `run` 中，`mode="disable"` 表示“自动触发已关闭”，但用户明确调用 `run` 仍执行 RSI 原子流程。`auto` 的模型判断是透明入口自动提交 Run 的前置门槛，不是召回门槛。若服务要求二次策略判断或审批，SDK 必须检查服务实际状态，不能凭本地决策越权。模型判断不是权限授权。

附件目标版的审批模式使用 `POST /v1/invocations`（`phase=prepare`、`mode=approval`），收到 `awaiting_approval` 后保存 `invocation_id`、`approval_id`、`context_hash`、`plan_hash`、`resume_token`，在原生执行之前抛 `RSIApprovalRequired`。管理员决定后，原调用方用同一 invocation 的 resume 继续，从获批后的 `prepared_context` 读取冻结 Recipe/Memory，再启动首次业务模型调用；不重新分类或新建 Run。旧直连地址尚无这组接口，**新网关是否部署必须单独核验**；能力缺失时 `approval` 在执行前报“服务能力不支持”，不能假装已审批。`auto` 收到远端要求审批的状态也要停止。若部署策略预先声明强制审批，分类失败不能退化成原生执行。

方式二只拼接原子接口，不调用 `/v1/invocations`；三个入口均使用 Friday 用户 API Key 经网关鉴权。若网关或后端拒绝 prepare/improve，则保留实际状态码并停止本次流程，不能私下切换协调器或内部凭证。透明 `auto` 命中使用原子流程；`approval` 需要附件中的协调器，尚待部署核验。

附件第 5 节还规定四个审批协调接口；它们不属于显式 `run` 的原子编排，也**未出现在旧直连地址的 OpenAPI 1.0.0 中**。透明入口 `approval` 要在目标网关确认这些接口和契约后才能启用，不应调用旧管理端审批 API 猜测等价行为：

| 协调器接口 | 用途 |
| --- | --- |
| `POST /v1/invocations` | 冻结 prepare/improve 阶段与 SessionContext，返回实际业务状态 |
| `GET /v1/invocations/{invocation_id}` | 查询准备、审批或改进状态；需要执行上下文时使用 `include_context=true` |
| `POST /v1/approvals/{approval_id}/decision` | 管理员批准或拒绝已冻结计划 |
| `POST /v1/invocations/{invocation_id}/resume` | 原请求主体消费批准，继续同一个 RSI 阶段 |

网关进程检查为 `GET https://friday-rsi-cn.stardust-ai.com/healthz`，无须 Key 且不带 `/api`；它不证明鉴权或后端可用。附件的后端 `GET /health/live` 与 `GET /health/ready` 是否经 `/api` 暴露需单独核验。Memory 的 `/memory/internal/v1/*` 是服务端内部协议，不是 SDK 对应用公开的蒸馏接口。

### 4.1 原生执行时每个远端接口的调用时机

下表描述**创建 Run 的原子流程**，只列服务端路径；目标完整 URL = `https://friday-rsi-cn.stardust-ai.com/api` + 表中路径，应用不传 CoSens 服务地址。例如 `GET /v1/runs/{run_id}` 对应 `GET https://friday-rsi-cn.stardust-ai.com/api/v1/runs/{run_id}`，`POST /internal/admin/v1/improvements/{job_id}/publish` 对应 `POST https://friday-rsi-cn.stardust-ai.com/api/internal/admin/v1/improvements/{job_id}/publish`，附件的后端健康路径若部署可对应 `/api/health/live`；网关自身健康检查是独立的 `/healthz`。附件的其他路径及查询参数也按相同规则转发，不能重复拼出 `/api/api/...`。**显式 `run(mode="disable")` 从第 1 步开始；透明 `auto` 只有判断命中才进入第 1 步；方式三在应用选择创建 Run 时显式调用第 1–3 步。**步骤按事件触发，没有后台定时上传全部历史。网关业务请求统一使用 `X-Auth-Type: api_key` 和 `Authorization: Bearer <Friday API Key>`；实际参数与大小上限以附件第 8 节及网关部署版本为准。

**不创建 Run 的流程**：每个新 turn 执行 `GET /v1/recipes/active?task_family=...` → `POST /v1/memory/recall`（query/token_budget）→ 验证响应 → 冻结 Recipe bundle、Memory evidence/refs/版本与召回 diagnostics → 注入首次业务模型 → 工具循环复用。`disable` 使用 general，`auto` 未命中时使用决策任务族。该流程不调用 prepare/get_run/session-events/submit，不推进游标；HTTP 错误和字段缺失不伪装为空结果。

| 顺序/时机 | 远端调用 | 发什么、保存什么 |
| --- | --- | --- |
| 0. 可选网关进程检查，非每次 run | 网关根路径 `GET /healthz`（不带 `/api`） | 无须 Key；200/ok 仅说明网关进程可用，不能证明用户 Key 或业务后端可用 |
| 1. 冻结本次任务后，只有存在需作为原始材料的文本时 | `POST /v1/artifacts` | 上传真实输入材料；保存 `artifact_id/content_hash`。**普通聊天消息不逐条伪装为文件** |
| 2. 每个新 RSI turn 的首次业务模型调用前一次 | `POST /v1/runs/prepare` | 服务端召回并冻结匹配的 Recipe/Memory；提交 `objective/task_family/input_artifact_ids/...` 和稳定 `Idempotency-Key`，保存 `run_id/recipe/context`。`processing` 按服务 `retry_after_seconds`（附件示例 2 秒）同键重试，不新建 Run；未 ready 不进入第 5 步 |
| 3. prepare ready 后、首次业务模型调用前一次 | `GET /v1/runs/{run_id}` | 读取完整冻结的 `snapshot/task_package_hash/snapshot_hash/recipe_revision`，与第 2 步的 Recipe/context 合成可追溯的注入上下文；读取失败不进入第 5 步 |
| 4. 原生模型/Agent 开始前 | `POST /v1/runs/{run_id}/session-events` | 只发送 Session 游标后的历史消息，1–200 条/批、每批最多 1 MB；逐条稳定 `id`；收到 `durable_ack` 后才前进游标。没有增量消息则不调用 |
| 5. 前置召回及必要消息 ACK 完成后，本地执行 | **无 RSI HTTP** | 第一种调用对应的原生 OpenAI 模型方法（例如 `responses.create` 或 `chat.completions.create`）；第二种调用原生 `Runner.run`；第三种由应用自行调用其模型/Agent。同一 turn 的工具循环复用第 2–3 步快照，不重复 prepare/get_run |
| 6. 原生新消息或工具结果实际产生后 | `POST /v1/runs/{run_id}/session-events` | 仅上报新增项，如 `agent_action/tool_result/session_end`；流式在事件完整且可持久化时分批上报，取消时不发完成事件。收到 ACK 再前进游标 |
| 7. 有文本交付物时 | `POST /v1/artifacts` | 上传实际输出文件并保存服务返回哈希；没有文件就不调用 |
| 8. 原生任务真正终止、结果已本地保存后 | `POST /v1/runs/{run_id}/submit` | `data` 包括真实 outcome/summary/输出 Artifact 引用；成功只表示结果记录，`evaluation=inconclusive`，不代表验收成功 |
| 9. 真实用户反馈到达时，可晚于 `run` 返回 | `POST /v1/runs/{run_id}/feedback` | 发送真实反馈，不从模型完成推断 accepted；若回执已有 improvement，复用其 `job_id` |
| 10. 有反馈或明确改进原因且没有现成 Job 时 | `POST /v1/runs/{run_id}/improvements` | 发送非空 `reason` 和稳定请求键，保存 `job_id`；没有学习依据时不调用 |
| 11. 已有 Job 且调用方要求观察进展时 | `GET /v1/improvements/{job_id}` | 有界轮询，不阻塞原生结果；建议从 2 秒退避到最多 30 秒，显式等待总时限默认 300 秒。以实时 Job 状态为准 |
| 12. Job 处于可恢复 awaiting 状态且缺口已补齐时 | `POST /v1/improvements/{job_id}/resume` | 只恢复原 Job，不重跑模型、不新建 Job；随后回到第 11 步 |
| 13. 有缺失的真实初始文件，且 Run 尚可补充时 | `POST /v1/runs/{run_id}/initial-inputs` | CN 网关 OpenAPI 有此接口，本次未单独写入验证；上传 Artifact 后提供 1–100 个 ID，不能拿最终答案冒充原始输入；冻结后必须新建 Run |
| 14. Job 已 validated 且管理员明确发布时 | `POST /internal/admin/v1/improvements/{job_id}/publish` | Friday 用户 Key、`expected_revision` 和发布原因；不由 `client.run` 自动调用。新 Recipe 只进入后续 Run |

第 4、6 步的 session-events 和 initial-inputs 路径已在新网关 OpenAPI 中存在；session-events 的字段、持久 ACK、重复事件回执已实测，initial-inputs 本次未单独写入验证。SDK 按已验证契约工作，不宣称初始化时自动进行能力协商。创建 Run 的冻结上下文须在业务模型前完成；独立召回使用公开 Recipe/Memory 接口。显式 run 不使用 trigger/decide、invocations 或管理端审批接口代替原子编排。

## 5. Session 消息增量上报与游标

游标由 SDK 持久化，键为 `(内部 CoSens 服务标识, RSI 调用主体, session_id, session_incarnation)`，值至少有 `acked_index`、`acked_message_id`、该项内容哈希、会话来源版本、最近一次 `run_id` 和待确认事件批次。内部服务标识随 SDK 管理的服务配置确定，不由应用传入；服务迁移时必须避免把旧服务的 ACK 游标误用于新服务。`session_incarnation` 用于区分同名 Session 被清空重建。不同 Session 独立并发；同一个 Session 的读取、上传、ACK 和游标提交必须串行，或以 compare-and-swap 防止旧请求倒退游标。

每次显式 `run`，或透明 `auto` 的 prompt 判断为“需要提交 RSI”后，SDK 读取当前 `session.get_items()`。只有索引大于 `acked_index` 的消息才形成待发批次；若宿主 Session 已有稳定消息 ID，使用它；否则用 `(session_id, session_incarnation, 原始索引, 规范化消息哈希)` 派生稳定 ID。必须验证游标之前的消息前缀仍与保存的 ID/哈希一致；若历史被截断、修改或重排，报 `SessionHistoryChanged`，要求宿主提供新 incarnation 或显式对账，不能悄悄从某个位置继续。

旧直连部署的 `POST /v1/runs/{run_id}/session-events` 接受 `{ "events": [...] }`，每项为 `{ "id": string, "kind": string, "payload": object }`；网关版本须核验。映射建议：用户消息为 `user_prompt`，assistant 消息为 `agent_action`，工具结果为 `tool_result`，可授权的上下文传递为 `context_delivery`，真实纠错为 `correction`，终态为 `session_end`。`payload` 保留 `session_id/message_id/role/index/content/source_revision` 等有来源字段；敏感数据按应用配置裁剪。服务返回的 `durable_ack` 是每条事件的持久化确认，SDK 只把**连续获 ACK 的前缀**写入游标。部分 ACK 重发未确认项；请求或响应丢失时使用同一 Run、同一事件 ID、同一内容重试。旧部署按 `(run_id, event.id)` 去重，同 ID 变内容会拒绝。

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

若透明 `auto` 连续几轮都不命中，游标保持原值；之后首次命中时，将这几轮中仍可从 Session 读取的未确认消息依序补发。SDK 不会因为某轮未触发而把它们标记为已发送。`disable` 与未命中 turn 的独立召回只读取所需上下文；召回请求及响应不代表聊天历史已上报，不能产生消息 ACK 或推进上报游标。

游标跨 Run 生效：先前 Run 已 ACK 的旧消息不会在下次 Run 再上传，新的 Run 只收到新的消息。远端的事件去重仅在单个 Run 内，**跨 Run 不重发主要由 SDK 本地持久游标保证**；如果本地游标和恢复日志全部丢失，当前服务没有按 Session 全局查询/去重的接口，无法证明“绝不重发”。为保证宕机恢复，SDK 在发请求前保存 Run ID 与待发事件，收到 ACK 后以事务提交游标；重启先恢复未完成 Run，不先创建新 Run。若部署要求多实例共享游标或丢失本地状态后仍严格去重，需要服务端新增按 `(主体, session_id, message_id)` 的全局 ACK/查询能力。

在创建 Run 的路径中，`user_input` 若尚未写入 Session，不作为“既有历史消息”提前伪造发送；它已经在本次 `prepare.objective` 中体现。原生调用后从 Session 的真实新增项采集并发送；若宿主不自动把新输入写入 Session，则由适配器从本次原生输入/输出建立有来源的事件，写入同一游标序列。未创建 Run 的路径不进行这些事件上报。透明入口如果只给 `previous_response_id` 或 `conversation`，没有可读 Session，无法凭远端引用拉取全部旧消息；此时只上报当前调用可见项，标识 `history.coverage=partial`。

消息事件写入**不等于** Memory 已完成蒸馏，也不会自动发布 Recipe。旧直连部署的回执只保证 `coverage=observable_events` 的可观测证据持久化；反馈、改进与独立验证仍按第 4.1 节走后续接口。现有接口按 Run 绑定事件，不能在新 Run 的 prepare 之前直接写入尚未归属的旧消息；因此“模型调用前召回”指在模型请求前取得服务端**当时已有**的 Recipe/Memory，不能保证刚上报的历史消息已被蒸馏并进入本次冻结快照。原生 Agent 自己的 Session 仍按原方式提供历史。若产品要求“先蒸馏所有历史，再让同一次 prepare 召回这些新记忆”，后端还需会话级预摄取接口或等价能力，当前已知 API 无法保证。

附件的 `SessionContext` 只适用于目标版协调器流程；本版显式 `run` 不把全量历史打包进每次 `/v1/invocations`。未来启用透明 `approval` 时，目标版 Schema 还需要接受原始 Responses 来源，例如 `provider="openai_responses_sdk"`，且明确 Agent 字段是请求描述，不是完整 Agent 实例。旧直连地址既无协调器，也无该来源协议；新网关未核验，不能宣称已支持审批入口。

## 6. 状态、恢复和错误

透明入口须持久化 turn 创建时的 `mode`。重试及工具续接只能在同一模式下复用决策；模式变化或旧记录缺少模式时抛出 `RSIRecoveryRequired`，在模型调用和远端写入前停止，防止旧 `auto` 决策绕过 `disable` 或 `approval`。

每个逻辑 turn 的本地状态至少含：决策结果（显式 `run` 可记为 `forced`，透明 `disable` 记为不自动提交）、冻结任务参数、`turn_id`、召回状态及 Recipe/Memory 来源与版本、原生执行状态、原生结果引用和最后错误。创建 Run 的路径还保存准备请求键、`run_id`、Session 游标、待发/已 ACK 事件 ID、提交请求键和反馈/Job 句柄；未创建 Run 时这些 Run 绑定字段可为空，不得为召回伪造 Run ID。存储方式由实现者选择，但必须能在同进程重试和重启后防止重复执行模型/工具；无法判定原生副作用是否发生时返回 `RSIRecoveryRequired` 交宿主对账。

当前原子流程区分 Run 的 `ready/completed`、事件的 `durable_ack`、Job 的 `queued/running/awaiting_*/validated/published/rejected/failed/deferred`。附件协调器的 `prepared/awaiting_approval/skipped` 属于另一个尚未部署的协议版本。`completed` 不等于验证通过；`validated` 需要管理员明确发布，且发布只影响后续新 Run。等待 Job 应有超时与退避，`resume_improvement` 仅用于指定 awaiting 状态。

HTTP 层保存状态码、领域错误码、detail 和请求 ID。旧直连部署 `1.0.0` 的 `Idempotency-Key` 只见于 `prepare` 和 `improvements`；附件 `2.1.0` 对 artifact/submit/feedback/publish 另有可选幂等键约定，不能未经核验套用到目标网关。`session-events` 依赖稳定事件 ID 与待核验的 `durable_ack`，不能假设其接受幂等 Header。旧部署的输出 Artifact 上传后若响应丢失，可能留下无法查询的孤儿 Artifact；submit 响应丢失时先 `GET Run` 读取状态与结果，已完成且匹配则记为成功，仍为 ready 才谨慎重试，不确定则进入 `RSIRecoveryRequired`。反馈和发布等无通用幂等保证的写请求也不得盲目重放。`prepare` 返回 `processing` 时用同一键轮询。回调或诊断写入失败不批准请求，也不重跑 Agent。

## 7. Python 模块划分与验收

Python 实现按职责拆为：`factory`（类型分派）、`responses_adapter`、`agents_adapter`、`decision`、`session_cursor`、`atoms/http`、`state`、`errors`；尚未部署的审批协调能力保留在设计中，当前 `approval` 明确报服务能力不支持。

验收至少覆盖：

1. 对已支持的 `openai` 包版本，`OpenAI/AsyncOpenAI`（及传入时的 Azure 变体）的公开资源和方法均可经代理调用；覆盖嵌套资源、`with_options/copy`、raw/streaming response、分页、文件、Realtime、上下文管理、关闭、自定义请求和原生异常。透明 `disable` 每个新 turn 有一次召回、分类器和自动 Run 请求数均为零。
2. 透明 `auto` 的 prompt 判断否/是/失败三个分支均在业务模型前召回；只有命中才增量上报并创建 Run，未命中不前进游标；工具续接不重复召回或 prepare。
3. 逐一验证被标为支持 RSI 的模型生成入口：普通、raw、流式、同步与异步调用均保持原生返回语义；不具备安全归一化能力的入口保留原功能并暴露 `rsi_capture=unsupported`。
4. 显式 `run(mode="disable")` 不请求判断模型，仍执行输入 Artifact → prepare → GET Run → Session 增量事件 → 原生 Agent → 新增事件 → 输出 Artifact → submit → 真实 feedback/improve → GET Job；每个 RSI 调用都是 `atoms.*`。
5. 同一 Session 多次 `run` 仅发送新消息；部分 ACK、同 Run 重试、重启恢复不丢消息或重复执行模型；历史截断/改写时明确报错。
6. 在目标网关检查 `/api` 后的实际接口版本及路径：`/api/v1/runs/prepare`、`/api/v1/runs/{run_id}`、`/api/v1/runs/{run_id}/session-events`、`/api/v1/invocations`（若启用 approval）及 `/api/internal/admin/...`（仅管理员场景）；旧直连地址 `1.0.0` 的能力不能直接套用。`approval` 在能力缺失时明确报错；管理员发布单独授权。
7. 原生完成后 HTTP 失败先对账再补交；无真实反馈不生成 `accepted`；未验证 Job 不发布；`observable_events` 不被误写成“Memory 已蒸馏完成”。
8. `cosens(...)` 与 `CoSensClient(...)` 的公开初始化参数均不包含 CoSens 服务 `base_url`；三个入口共享内部传输配置，且不覆盖原生 OpenAI client 自己的 `base_url`。
9. 用调用顺序记录验证三种入口：每个新 turn 的首次业务模型前均取得 Recipe/Memory；创建 Run 时用 `prepare → get_run`，不创建 Run 时组合已验证的公开 Recipe 与 Memory 接口；同一 turn 的多次模型调用只召回一次，下一新 turn 重新召回。`auto` 未命中/决策失败与透明 `disable` **仍召回**且不自动创建 Run；召回失败不能伪装成空结果发起业务模型调用；合法 `not_found`/空 Memory 可以继续。
10. 所有接口仅在内部网关前缀 /api 后拼接一次，保留查询参数与幂等语义；CoSens 请求发送 `X-Auth-Type: api_key` 与用户 Key 的 Bearer 头，不接受/发送 X-User-Id 或 X_User_Id，由网关注入身份。三个入口均不请求内部 Memory 协议。实际已验证范围见联调报告。

## 8. TypeScript 复刻规范（基于当前 Python 工作区）

本节是 TypeScript 实施输入，目标为**行为与 HTTP 协议等价**，不是逐行翻译 Python。核对基线为 `python/src/cosens/`、`python/tests/` 和本文件第 1–7 节。服务端字段及路径以已实测的网关契约为准；Python 特有的同步方法、`httpx`、`sqlite3`、`contextvars`、`agent.clone()`、`with_raw_response` 均不能当作 JS SDK 的现成接口。先实现 Node.js 服务端使用场景，使用方持有 Friday Key；浏览器直接持有 Key 和可靠跨重启状态不在本版范围。工作区已锁定 `openai@7.45.0` 与 `@openai/agents@0.21.0` 并开始实现，以下 API 形状仍作为完整交付契约；已有代码和测试覆盖的范围以 `typescript/README.md` 与实际验收结果为准。

### 8.1 三层公开入口与命名

```ts
type CoSensMode = 'auto' | 'approval' | 'disable';

// 公开调用形状；具体类型由锁定的 TS 依赖版本与 CoSens 代理类型确定。
const client = cosens(openai, { apiKey, mode: 'auto', session, statePath });
const response = await client.responses.create({ model, input: '用户任务' });
const turn = await client.cosens.getTurn(client.cosens.lastTurnId);

const runner = cosens(agent, { apiKey, mode: 'disable', session, statePath });
const nativeResult = await runner.run(agent, '用户任务', { session });

const rsi = new CoSensClient({ apiKey, statePath });
const prepared = await rsi.atoms.prepare({ objective: '用户任务', key: stableTurnKey });
```

对外选用 TypeScript 常用的 `apiKey/statePath/getTurn/lastTurnId/recallContext` 等 camelCase 名；HTTP JSON、响应体与事件 payload 保持服务端 `snake_case`，不得把 `run_id`、`task_family`、`durable_ack` 改名后发往网关。公开 `cosens(OpenAI)` 返回原生客户端全部公开资源均可调用的代理，保留原生请求选项、返回值、错误、流生命周期和派生客户端能力；只拦截经白名单验证的生成入口。`client.native` 提供原对象，绕过包装的原生调用不会被采集。`client.cosens` 暴露 `mode/session/lastTurnId/rsiCapture/getTurn/reconcile/scope`；`scope` 仅设置本地 `sessionId/turnId` 边界，不改原生请求参数。重复包装同配置返回既有代理，冲突配置报错。关闭派生客户端时只在最后一个包装拥有者退出后关闭共享 CoSens 资源；原生资源仍按其自身生命周期关闭。对 TypeScript 原生客户端是否提供 `copy/withOptions`，按锁定版本实际 API 适配，不能凭 Python 名称假定存在。

`cosens(Agent)` 返回显式 runner，`run` 返回原生 `RunResult`，`runStreamed` 返回原生可迭代流的包装，`pendingState/resume` 处理原生工具审批。`mode='disable'` 仍执行显式 RSI Run。原子 `CoSensClient` 只请求 CoSens 服务，不执行模型或工具；`atoms` 和 `admin.atoms` 均为异步 Promise API。Python 的内部 `sync_atoms` 只是同步 OpenAI 代理所需，不作为 TypeScript 必须公开的入口。三层都可关闭自己的网络和状态句柄。

### 8.2 服务、认证和 HTTP 原子能力

当前 Python 工作区的内部区域配置为 `COSENS_REGION=cn`（默认，`https://friday-rsi-cn.stardust-ai.com/api`，持久化身份 `friday-rsi-cn.stardust-ai.com/api`）和 `COSENS_REGION=us`（`https://rsi.cosens.ai/api`，身份 `rsi.cosens.ai/api`）。TypeScript 使用同一允许列表和默认值，不提供任意 CoSens `baseURL`；构造时固定区域，单个实例运行期间不随环境变量变化。**仅 CN 网关有第 4.1 节的真实联调记录**，US 必须在发布对应区域支持前单独验证。原生 OpenAI 客户端的模型 `baseURL` 与这里完全独立。服务相对路径只允许以单个 `/` 起始的路径，拒绝绝对 URL、`//`、重定向目标和外部主机；拼接时保留 `/api` 恰好一次及原查询参数，资源 ID 按 URL 路径段编码。

构造选项 `apiKey` 优先；未传时按 `RSI_API_KEY`、`COSENS_API_KEY` 顺序读取，必须是无空白的 Friday 用户 Key。每个 CoSens 业务请求同时发送 `X-Auth-Type: api_key` 和 `Authorization: Bearer <api_key>`；有明确幂等键的接口再发送 `Idempotency-Key`。这两个鉴权头只属于 CoSens 传输，不能注入原生 OpenAI 模型请求。不得发送 `X-User-Id`、`X_User_Id`，不得把 Key 写入 URL、JSON、日志或持久化状态。TLS 验证开启，不跟随重定向，默认 CoSens 超时 30 秒。2xx JSON 解析成原字段对象；4xx/5xx 保存状态码、`error.code`、`error.message` 或 `detail`、`x-request-id`；3xx、非 JSON 2xx 和网络失败分别抛有类型的 HTTP/传输错误。不要把异常吞掉后继续调用业务模型。参照 `http.py`、`errors.py`、`_config.py`。网络超时不等于远端操作未发生。

TypeScript 错误类别与 Python 的 `RSIHTTPError(statusCode,code,requestId)`、`RSITransportError`、`RSIDecisionUnavailable`、`RSIApprovalUnavailable`、`RSIRecoveryRequired`、`SessionHistoryChanged` 保持可区分；原生 OpenAI/Agents 的错误原样传播。CoSens 错误文本不得包含完整 Bearer Key。`scope` 在并发异步调用中用每条异步执行链的上下文保存，不能用进程全局变量混合两个用户的 `turnId`。

下表为 **完整当前 Python 原子方法**，TypeScript 方法名用 camelCase；请求体/响应字段仍以 `atoms.py` 和网关 OpenAPI 为准。`runId/jobId/...` 均需校验为非空字符串并作为单个路径段编码；`key/requestKey` 为 1–256 字符非空值。`prepare` 和 `improve` 的 key 是已使用的幂等契约；`submit/feedback` 当前客户端可传 key，但发布时必须按目标网关实际支持情况验证其语义。

| TS 方法 | HTTP 路径（均在区域 `/api` 后） | 请求与边界 |
| --- | --- | --- |
| `recallContext({query,taskFamily='general',tokenBudget=1000})` | 下两项顺序组合 | 不创建 Run；校验 Recipe `matched/not_found`、匹配时 `recipe_id/bundle`、Memory `items` 数组，冻结 `{recipe,context,snapshot}` |
| `getActiveRecipe({taskFamily})` | `GET /v1/recipes/active?task_family=...` | 保留查询参数 |
| `memoryRecall({query,tokenBudget})` | `POST /v1/memory/recall` | `{query,token_budget}` |
| `memoryIngest({content,requestKey,sourceKind='decision',durable=true})` | `POST /v1/memory/ingest` | `request_key` 在 JSON 正文中 |
| `getMemoryJob(jobId)` / `getMemoryExperience(id,{revision})` / `getMemory()` | `GET /v1/memory/jobs/{id}` / `GET /v1/memory/experiences/{id}?revision=...` / `GET /v1/memory` | 只读；revision 可省略 |
| `artifact({name,content})` | `POST /v1/artifacts` | 只上传真实文本材料，保存 `artifact_id/content_hash` |
| `prepare({objective,key,taskFamily='general',inputArtifactIds=[],...options})` | `POST /v1/runs/prepare` | 正文为 `objective/task_family/input_artifact_ids/...`；key 放 Header |
| `getRun(runId)` / `getRecipeStep(runId,nodeId)` | `GET /v1/runs/{id}` / `GET /v1/runs/{id}/recipe/steps/{node}` | 准备完成后的快照，或恢复对账 |
| `sessionEvents(runId,events)` / `initialInputs(runId,artifactIds)` | `POST /v1/runs/{id}/session-events` / `POST /v1/runs/{id}/initial-inputs` | 正文分别为 `{events}`、`{artifact_ids}`；事件按持久 ACK 处理 |
| `guidance(runId,{data})` / `checkpoint(runId,{data})` | `POST /v1/runs/{id}/guidance` / `POST /v1/runs/{id}/checkpoint` | `{data}`；不得把建议当成工具已经执行 |
| `getDecision(runId,decisionId)` / `getTrace(runId)` | `GET /v1/runs/{id}/decisions/{decisionId}` / `GET /v1/runs/{id}/trace` | 只读 |
| `submit(runId,{data,key?})` / `feedback(runId,{data,key?})` | `POST /v1/runs/{id}/submit` / `POST /v1/runs/{id}/feedback` | `{data}`；submit 不代表验收通过，反馈必须有真实来源 |
| `improve(runId,{reason,key?})` | `POST /v1/runs/{id}/improvements` | `{data:{reason}}`，reason 非空；已有反馈自动 Job 时复用 |
| `getImprovement(jobId)` / `resumeImprovement(jobId)` / `waitImprovement(jobId,{timeout=300})` | `GET /v1/improvements/{id}` / `POST /v1/improvements/{id}/resume` / 有界轮询 GET | `queued/running` 才继续等待；初始 2 秒、最多 30 秒退避 |
| `admin.atoms.bootstrapBundle({taskFamily,bundle,reason})` | `POST /internal/admin/v1/recipes/bootstrap-bundle` | `{task_family,bundle,reason}`；显式管理动作 |
| `admin.atoms.evaluateDecision(runId,decisionId,{evaluation})` / `evaluateRecipe(runId,{record,artifactId,contentHash})` | `POST /internal/admin/v1/runs/{id}/decisions/{decisionId}/evaluations` / `POST /internal/admin/v1/runs/{id}/recipe-evaluation` | 分别发送 evaluation 原对象、`{record,artifact_id,content_hash}` |
| `admin.atoms.publish(jobId,{expectedRevision,reason})` | `POST /internal/admin/v1/improvements/{id}/publish` | `{expected_revision,reason}`；显式管理员调用，不由包装入口自动触发 |

`prepare` 返回 `processing` 时同 key 重试，等待 `retry_after_seconds`（缺省 2 秒，钳在 0–30 秒），总时限默认 300 秒；只在 `ready` 且有 `run_id` 时继续。随后 `getRun` 必须返回同一个 `run_id`、`status=ready`、对象形的 `snapshot`，而 `prepare.recipe/context` 必须是对象；深拷贝组合后才可调用首次业务模型。独立召回要求没有 `run_id` 且 `recipe/context/snapshot` 均为对象；合法 Recipe `not_found` 与空 Memory `items` 可以继续。原子客户端本身不强制这些编排顺序，包装入口必须强制。参照 `workflow.py`、`atoms.py`。

独立召回的**本地归一化结果**应与 Python `_recall_context` 一致：`{recipe:<原 GET 响应>,context:{query,task_family,source:'public_recipe_and_memory'},snapshot:{evidence:<POST 响应的 items>,memory_recall:<原 POST 响应>}}`。这是 SDK 内部结构，不能把它误写回两个原子 HTTP 请求。创建 Run 时的本地归一化结果以 `getRun` 响应为底，深拷贝加入 `prepare.recipe` 与 `prepare.context`，保留 `run_id/status/snapshot`。两者都要在同一 turn 的后续模型调用中复用。

### 8.3 透明 OpenAI JS/TS 客户端

只在应用调用经过 CoSens 代理的客户端时拦截。实现使用动态资源代理保留未知资源的原生可用性，但**支持 RSI 采集的生成方法要逐项列白名单并测试**。以 Python `responses_adapter.SUPPORTED` 为行为基线：`responses.create/parse/stream`、`chat.completions.create/parse/stream`；旧 `completions.create` 虽在白名单中，当前 Python `openai_proxy.py` 实际标为 `rsi_capture=unsupported` 后透传，TypeScript 不能把它宣传为已采集。JS SDK 没有 Python 的 `with_raw_response/with_streaming_response` 命名链；按锁定 JS 版本实际提供的 `APIPromise.asResponse()/withResponse()`、`responses.create({stream:true})`、`responses.stream()` 适配。返回的 Promise 特有方法、响应头、原生错误、取消信号和异步事件顺序必须可用；不支持安全归一化的生成调用透传并设置 `rsiCapture='unsupported'`。文件、分页、Batch、Realtime 与管理资源透传，不把它们误当作一个完成的 RSI turn。Python 同步客户端行为不需在纯异步 TS API 中伪造。

每个新 turn 的执行序列由 `mode` 决定：

1. `disable`：不分类；以当前可见用户文本为 query、`general` 为 task family 执行独立召回，冻结上下文；不创建 Run、上报事件或推进 Session 游标。
2. `auto/approval`：用**未包装**原生客户端的 Responses API 做结构化分类，字段为 `activate:boolean/objective:string/task_family:string/reason:string`，`additionalProperties=false`；只给当前请求最后一条可见用户消息的文本末尾 8,000 字符和工具名称，不转发系统指令、工具结果、文件正文或模型工具权限。默认模型为业务请求的 `model`，可用 `decisionModel` 覆盖；两者都缺失时在请求前报配置错误。默认超时 5 秒、输出 256 token。`auto` 在已选定模型后的分类调用失败时记录 `decision_failed` 并走第 1 步召回；`approval` 分类失败报 `RSIDecisionUnavailable`。`activate=false` 以分类得到的 task family 独立召回；`activate=true` 按第 4.1 节创建 Run 并提交。`approval` 命中时当前已验证网关无协调器契约，业务模型前报 `RSIApprovalUnavailable`，不得直接执行或伪造批准。
3. Run 路径先 `prepare → getRun → ACK 既有 Session 消息 → 注入 → 原生生成`，原生结果完成后上报可观测新事件，再 `submit`。无 Run 路径 `getActiveRecipe → memoryRecall → 注入 → 原生生成`。即使独立召回为空也要有合法响应；失败、缺字段、无法安全注入均阻止业务模型。非对话资源无需召回。每个 turn 的后续工具循环复用冻结内容；只有新用户 turn 再召回。

分类模型的结构化响应还须在 SDK 本地校验：恰有 `activate/objective/task_family/reason` 四个字段，类型分别为布尔值和三个字符串；缺字段、额外字段或类型错误均按分类失败处理，不能依赖模型服务一定遵守 JSON Schema。

仅从安全可见的原生请求字段提取用户文本。Responses `input` 的字符串或 `role=user` 文本项、Chat `messages` 的用户文本项可作为 query；不能把非文本多模态内容转成虚构文本。注入时复制请求，不改调用者对象：Responses 在文本 `instructions` 尾部附加 `[CoSens frozen context; source: ...]` 与 `{recipe,snapshot,context}` JSON；Chat 在原消息后附加 `developer` 消息；非文本 instructions 或不可安全构造的输入先报错。不得替换原系统指令、工具参数或排序。TypeScript 的 `previous_response_id` 与 Python 的 `previous_response_id` 语义映射应以锁定 JS 类型为准；同一响应链的 function call/output、会话引用、Chat `tool_call_id` 或显式 scope 关联一个 `turnId`，不得以每次 HTTP 调用都新建 Run。无法可靠关联的工具请求保持 `awaiting_continuation`，不提交。

流式路径在打开原生流前完成分类与召回。Responses 只在收到真正的 `response.completed` 且终态中没有待处理 function call 时提交；Chat 要有最终 `finish_reason` 且不是工具调用。调用方 `AbortSignal` 在召回期间触发时不得启动业务模型；取消、提前退出、网络断开，以及原生 `status=incomplete` 或没有可续接工具调用的非终态结果，均记 `incomplete`；只有已完成的工具调用请求记 `awaiting_continuation`，不得伪造 `submit`。`done()`、`finalResponse()` 和异步迭代必须保留原生终态异常；即使原生 `done()` 返回成功而最终响应失败，也不能吞掉失败或保留 `native_running`。raw Response 的 body 若调用者从未解析，不能凭 HTTP 头判定模型已完成。非流式返回对象与原生异常原样交还；后置 CoSens 失败持久化为 `completion_pending`，通过 `getTurn/reconcile` 查补，不能重新发模型请求。参照 `openai_proxy.py`、`responses_adapter.py`、`decision.py`。

### 8.4 显式 Agents JS/TS runner

以同一个顶层 `run(agent,input,{session,...nativeOptions})` 为一个逻辑 turn。Agent 运行、工具、handoff、guardrail、流与原生审批由 Agents SDK 执行；CoSens 在外层编排，不替换工具结果。读取宿主授权的 Session 项并冻结增量 → 上传 `inputArtifacts` 中真实 `{name,content}` 文本 → `prepare`（turnId 作 key）→ `getRun` → ACK 旧消息 → 在本次调用的有效指令中注入冻结上下文 → **只调用一次原生 Runner** → 保存最终原生结果 → ACK 新消息 → 上传 `finalOutput.files` 中真实文本交付物 → `submit`。无 Session 时仍可 prepare/submit，但不声称有完整聊天事件。`mode='disable'` 跳过分类，仍执行上述显式流程；用户反馈、improvement 与发布仍需显式后续调用。

JS Agents 当前以 `run(agent,input,{session,stream})` 或 `Runner.run(...)` 运行；Session 的 `getItems/addItems`、`finalOutput` 和 `StreamedRunResult.completed` 与 Python 命名和写入时机不同。使用经验证的每次运行指令过滤器或不可变 Agent 副本注入，不能修改并发共享 Agent；必须保证多次模型调用都见到同一冻结上下文且不会重复追加。流消费结束并确认 `completed` 后才能提交；提前取消/异常不提交。原生工具审批中断保存其可恢复 `RunState`，`pendingState/resume` 继续**同一** CoSens Run 和冻结上下文，不重新 prepare 或分类；原生工具审批不能当作 RSI 协调器审批。实际方法签名以锁定的 Agents JS 版本核准，并在测试里证明 `session` 前后读取时机与事件顺序。参照 `agents_adapter.py`。

### 8.5 Session 游标、状态和跨语言边界

TypeScript 先定义持久状态接口，Node.js 默认实现须支持崩溃恢复、同 Session 串行化和事务/CAS；不能用进程内 Map 充当默认持久游标。可选 SQLite，但不要求直接读取 Python 的 `~/.cosens/state.sqlite3`：**跨语言共享远端事件语义，默认不共享本地数据库文件或 Python 的 `default=str` JSON 哈希**。若产品以后要从 Python 进程切换到 TypeScript 进程且沿用原 Session，必须先定义并验证版本化状态迁移或服务端 Session 全局 ACK 查询；直接各用全新空游标会重发已上报历史。存储 key 必须分隔 `(内部 service identity, 调用主体的不可逆标识, sessionId, sessionIncarnation)`；Key 原文不得持久化。保存 turn 创建时的 `mode`，重试/工具续接模式不同或未知时停止。区域切换不得复用旧区域状态。

宿主 Session 适配器至少提供稳定 `sessionId`、`getItems(): Promise<readonly item[]>`，可选 `sessionIncarnation`（默认 `'0'`）；将实际 Agents JS Session 项转成稳定、可序列化的事件项。事件形状与第 5 节一致：`{id:'chat:<stable-id>',kind,payload:{session_id,message_id,index,role,content,source_revision}}`，已有稳定消息 ID 优先；否则用 session key、原始索引与规范化项内容派生。同一 Session 内重复的消息 ID 必须在发送前报 `SessionHistoryChanged`，否则单个 ACK ID 可能错误地确认多条消息。在同一存储实现里固定 UTF-8 JSON 规范化与哈希算法，并用 fixture 锁定，避免升级后改变 ID。每批最多 200 条、1,000,000 UTF-8 字节；单项过大报错。ACK 只推进连续前缀，部分 ACK 保留同一 Run 的剩余项；发送前原子保存 `runId + pendingEvents`，回执丢失后以相同 Run、相同事件 ID 和相同内容重试。Session 游标与透明代理的可观测事件回执都只能确认本批实际发送的唯一事件 ID；额外或重复 ID 必须保留待发送批次并停止提交，不能污染已确认集合。ACK 前后都验证已确认历史前缀未截断/改写；冲突报 `SessionHistoryChanged`。同 Session 并发必须加锁或用 CAS，跨进程不能仅依靠进程内锁。无 Run 的 turn 不写事件、不推进游标。参照 `session_cursor.py`、`test_session_cursor.py`。

turn 状态至少包含 `turnId/mode/decision/objective/taskFamily/runId/prepared/frozenContext/requestKeys/observablePending/observableAckedIds/nativeState/result/lastError` 以及输出 Artifact 的上传进度；允许无 Run 字段为空。创建/上传/执行前先落盘对应阶段。`pre_execution_pending` 可用原 key 和原输入 Artifact 清单恢复远端准备；重试传入不同 Artifact 必须在网络请求前报恢复错误；进入 `native_running` 后，若是否执行过模型/工具不明，报 `RSIRecoveryRequired`，绝不隐式重跑。原生完成先保存 `result`，再发送事件/输出/submit；HTTP 失败转 `completion_pending`，`reconcile` 先补 ACK、读 `getRun`，远端已 `completed` 且结果按 JSON 结构比较一致（字段顺序无关）则本地完成，远端 `ready` 才谨慎补 submit，其他情况报恢复错误。Artifact 上传回执的 `artifact_id/content_hash` 缺失或回执不明时不得盲传第二份，待上传标记必须跨重试保存。流取消标 `incomplete`，工具续接标 `awaiting_continuation`，原生审批标 `awaiting_native_approval`。参照 `state.py`、`openai_proxy.py`、`agents_adapter.py`。

原生 Agent 的 `finalOutput.files` 若存在，必须是 `{name,content}` 文本对象数组。格式错误时保留已完成的原生结果和原始文件字段，置 `completion_pending` 并停止 Artifact 上传和 submit；`reconcile` 应重复报告格式错误，不得把文件静默丢弃或重新执行 Agent。

### 8.6 实施顺序与验收矩阵

建议按**原子客户端 → 持久状态/游标 → 透明 OpenAI 代理 → 显式 Agent runner → 真实网关联调**落地。`typescript/src/` 的建议职责划分为：`config.ts`（区域允许列表）、`http.ts`/`errors.ts`（双鉴权头传输与错误）、`atoms.ts`（所有原子映射）、`decision.ts`（结构化分类）、`context.ts`（召回校验与注入）、`cursor.ts`（Session ACK）、`state.ts`（turn 恢复）、`openai-proxy.ts`（动态资源与流适配）、`agent-runner.ts`（显式 Agents 编排）、`index.ts`（公开入口）。`typescript/tests/` 按上述边界建立对应测试，并以第 8.2 节映射表和 Python 测试行为为 fixture。每一层使用模拟 HTTP 与原生 SDK 替身先验收；真实服务只用授权 Key 和独立测试标识。以下是 TypeScript 交付门槛，测试需记录 HTTP 次序、头与正文以及原生执行次数，而不只检查最终文本：

| 场景 | 必须观察到的结果 | Python 对照 |
| --- | --- | --- |
| CN/US 配置、无效区域、路径/认证 | 固定允许列表，区域隔离状态；`/api` 只拼一次；CoSens 请求同时携带 `X-Auth-Type: api_key` 和 Bearer；拒绝外部 URL、重定向与身份伪装头 | `_config.py`、`http.py`、`test_gateway.py`、`test_regions.py` |
| 公开原子能力 | 第 8.2 节所有方法的路径、body、查询参数、ID 编码、错误诊断准确；`prepare processing` 同 key 等到 ready | `atoms.py`、`workflow.py`、`test_atoms.py`、`test_gateway_contract.py` |
| 无 Run 召回 | Recipe `matched/not_found`、Memory 空/非空；两次读取一次冻结；错误/畸形响应阻断业务模型 | `test_independent_recall.py`、`test_turn_recall.py` |
| 透明 mode 三分支 | `disable` 零分类零 Run；`auto` 命中一 Run、未命中/分类失败无 Run；`approval` 命中明确停止；所有可执行业务 turn 先召回 | `test_openai_proxy.py`、`test_turn_recall.py` |
| 透明兼容与流 | 资源树透传、原生模型 URL/返回/错误/选项、Promise raw API、完整事件序列；终态才 submit，工具续接复用快照，未知生成入口标 unsupported | `test_openai_proxy.py` |
| 显式 Agent | 原生 Runner 一次；Session 旧消息 ACK 在执行前，新消息 ACK 与输出 Artifact 在 submit 前；流/审批恢复使用同 Run | `test_agent_runner.py` |
| ACK 与恢复 | 同 Session 跨 Run 只发新增；部分 ACK、丢回执、改写历史、并发和进程重启均不跳项、不重跑原生副作用 | `test_session_cursor.py`、`test_agent_runner.py` |
| 真实网关 | CN 的公开 Recipe/Memory 与原子主链可按独立测试标识复现；US 及 approval 单独核验后才标支持 | `2026-10-27-gateway-validation.md` |

文档与示例需明确 TypeScript 的 Node.js 版本下限、锁定的 `openai`/`@openai/agents` 版本、状态文件位置、关闭方法、Key 注入方式、三种入口和恢复手册。已在本地对锁定版本的六个已适配生成入口（Responses/Chat 的 `create/parse/stream`）、`OpenAI.withOptions()` 派生客户端、`APIPromise.withResponse()/asResponse()`、只读资源的原生错误、Agents `Runner` 和 `StreamedRunResult.completed` 进行运行时兼容测试；OpenAI 的 27 个资源根和 399 个公开方法已固定清单，393 个非采集方法逐项验证代理参数、返回值和原生异常透传。原子能力已逐项核对路径/正文；TypeScript 与 Python 的准备及 Improvement 轮询会在请求前拒绝非有限或负超时，异常 `retry_after_seconds` 按 2 秒回退，turn 主记录与请求/逻辑恢复别名已使用同一 SQLite 事务落盘，透明代理和显式 Agent 的同 Session SQLite 租约已用双实例测试，独立进程退出后的租约重获也已验证；显式 Agent 的恢复键按 Session incarnation 分隔，审批恢复和对账会校验原 Session 身份；锁定版 Agents `MemorySession` 的两次运行已验证跨 Run 增量 ACK；真实 `Agent` 的两次模型调用和一次工具执行已验证同一冻结上下文只注入一次；锁定版 Agents 的工具审批 `RunState` 已验证序列化、`getInterruptions()` 审批及同 Run 恢复，普通与流式入口均已覆盖。流取消已覆盖召回期间及原生流启动之后；延迟流的 `emitted()` 与 `events()` 会在预处理期间登记监听器，避免遗漏原生流启动后的首事件；`events(error)` 保留原生以事件值交付错误的语义，工具调用终态不会 submit，原生 `incomplete` 不再误作工具续接；`conversation` 工具续接已覆盖请求传入和服务端返回的会话 ID；工具调用响应及续接别名会先持久化，可观测事件发送前保存待 ACK 批次；TypeScript 与 Python 同步/异步入口均验证了模型完成后 Session 读取失败的跨客户端对账；最终结果的对账会用已保存摘要重建未发送的用户输入和模型输出事件，再补 submit；Chat `tool_call_id` 续接已覆盖有 Run、无 Run 和流式工具调用分片三种路径；锁定版 OpenAI Chat `.stream()` 的真实对象已验证同 Run 续接及工具事件在下一次模型请求前上报。2026-10-28 的 TypeScript CN 实网记录已验证双鉴权头、独立召回、包装入口合成 Run 和原子主链，见 [`typescript/docs/2026-10-28-gateway-validation.md`](../../typescript/docs/2026-10-28-gateway-validation.md)。文件二进制下载、文件、音频转录与 Uploads Part 的 multipart 上传、Batch 分页、Realtime SDP，以及 Embeddings、Moderations、Images、Vector Stores 和管理端 Projects 的实际请求与原生认证已用锁定版原生客户端及进程内模型传输验证；Fine Tuning、Webhooks、Beta Agents、Evals、Containers、Skills、Videos、Audio Voice Consents 和 Conversations 也已验证只读请求。仍须验证其余原生方法的实际网络行为、透明流的全部生命周期分支，并单独验证 US。不得仅凭现有测试和 CN 联调就宣称 TypeScript 完整兼容。

## 9. 待产品/后端确认的接口决策

1. CN 网关 `https://friday-rsi-cn.stardust-ai.com/api` 的 Bearer 鉴权、转发路径、公开 Recipe/Memory 与 `session-events` 的 `events/durable_ack` 已在 2026-10-27 联调；2026-10-28 TypeScript 复验了新增 `X-Auth-Type: api_key` 与 Bearer 双请求头、公开召回及原子主链。OpenAPI 仍标识 `1.0.0`，与附件 `2.1.0` 有差异。US 区域 `https://rsi.cosens.ai/api` 尚无有效 Key 的同等联调记录，不能因内部允许列表存在就声称其完整兼容。`/v1/invocations` 协调器仍未验证，不得推出 approval 已可用。
2. 若要求跨 Run、跨实例、丢失本地状态后仍绝对不重发消息，后端需要 Session 级全局消息 ID 去重和 ACK 查询；CN 网关联调验证了同 Run 重复事件 ACK，没有验证跨 Run 全局去重。
3. 若要求“刚上传的历史消息立即影响本次 prepare 的 Memory 召回”，后端需要 prepare 前的会话级摄取；已知 `session-events` 只能在获得 run_id 后调用。当前设计保证调用业务模型前召回服务端已有内容，不保证新历史在同次 prepare 前完成蒸馏。
4. 透明 `approval` 需要网关部署附件协调器，并扩展 `openai_responses_sdk` 的 SessionContext 来源；旧直连地址不能执行该模式，网关能力待核验。
5. 独立召回已由公开 Recipe 查询与 Memory 召回组合实现；后端若需强事务一致快照，可未来新增统一召回接口。当前 SDK 保留两次读取结果并在同一 turn 内冻结，不重复召回。

## 10. 官方参考

- [OpenAI Python Agents SDK 运行循环](https://openai.github.io/openai-agents-python/running_agents/)
- [OpenAI Python SDK 公开客户端及调用方式](https://github.com/openai/openai-python/blob/main/README.md)
- [OpenAI JavaScript/TypeScript SDK 公开客户端与原始响应](https://github.com/openai/openai-node/blob/main/README.md)
- [OpenAI JavaScript/TypeScript SDK 流式响应](https://github.com/openai/openai-node/blob/main/docs/streaming.md)
- [OpenAI Agents JS 运行、Session 与流式接口](https://openai.github.io/openai-agents-js/guides/running-agents/)
- [OpenAI Agents JS Session](https://openai.github.io/openai-agents-js/guides/sessions/)
- [OpenAI Responses API 的结构化输出](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
- [OpenAI Responses API 流式事件](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI Responses API 会话状态](https://developers.openai.com/api/docs/guides/conversation-state)
