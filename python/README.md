# CoSens Python SDK

首版 Python SDK 实现三种入口：透明包装 OpenAI client、显式执行 Agents SDK Agent，以及直接调用 RSI 原子接口。当前服务端地址由 SDK 内部管理，公开初始化参数没有 CoSens `base_url`。

## 安装

```bash
pip install ./python
# 使用 Agents SDK 时：
pip install './python[agents]'
```

要求 Python 3.10+、`openai` 2.54.x；可选 Agents SDK 为 0.20.x。当前测试环境为 `openai==2.54.0`、`openai-agents==0.20.0`。通过初始化参数 `api_key` 或环境变量 `RSI_API_KEY` 提供 Friday 用户 API Key；也兼容 `COSENS_API_KEY`，优先级为显式参数、`RSI_API_KEY`、`COSENS_API_KEY`。只填 Key 本身，SDK 自动生成 `Authorization: Bearer <key>`，不使用 `api-key` 或 `X-API-Key` 请求头，也不需要后端服务令牌。旧的 `token`、`admin_token` 参数已移除，统一使用 `api_key`。CoSens Key 不传给 OpenAI 模型服务。

所有业务接口由内部地址 `https://friday-rsi-cn.stardust-ai.com/api` 转发。网关进程健康检查为域名根路径 `/healthz`，不属于 `/api` 业务前缀。鉴权依据见 [网关调用指南](/Users/a1111/IdeaProjects/friday-rsi/docs/api/gateway-authentication.md)。

密钥采用 **HTTPS + Bearer**：Key 在应用进程中作为字符串使用，网络传输中的请求头和正文由 TLS 加密，SDK 保持证书验证开启。Key 只放在 Authorization 头，不放 URL 或业务 JSON；网关传输拒绝绝对/外部 URL，不跟随重定向。此方式遵循 [RFC 6750](https://www.rfc-editor.org/rfc/rfc6750#section-5.3) 的 Bearer 传输要求，不对 Key 另做 Base64、哈希或自定义加密。示例从环境变量读取，生产环境可通过部署平台的密钥管理功能注入；不要将真实 Key 写入源码或日志。SDK 不自动加载 `.env` 文件，仓库已忽略 `.env` 与 `.env.*`（示例文件除外）。

SDK 持久化游标与恢复记录默认写在 `~/.cosens/state.sqlite3`，也可传 `state_path` 指定本地文件。记录按服务及调用凭证摘要隔离，旧直连服务的 Run 和 ACK 不会自动迁移至网关。

**当前实现状态（2026-10-27）**：国内网关真实联调已覆盖 Memory 写入/召回、Recipe、Run、guidance/checkpoint、消息 ACK、反馈及经验评估。`atoms.recall_context(query=...)` 组合公开的 Recipe 查询与 Memory 召回，不创建 Run；透明 `disable`、`auto` 未命中/分类失败均可使用。每个 turn 冻结一次结果。网关自动注入 `X-User-Id`，SDK 不提供也不发送该身份头。验收范围与真实请求记录见 [网关联调报告](docs/2026-10-27-gateway-validation.md)。

## 方式一：透明包装 OpenAI SDK

```python
import os
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(native, api_key=os.environ["RSI_API_KEY"], mode="auto")
response = client.responses.create(model="your-app-model", input="用户任务")
print(response.output_text)

turn_id = client.cosens.last_turn_id
if turn_id:
    print(client.cosens.get_turn(turn_id))
client.close()
```

`mode` 只控制自动 Run：`auto` 由独立模型决定是否发送，`disable` 关闭自动发送。两者都要求每个 turn 首次业务模型调用前召回 Recipe/Memory，工具循环复用同一份结果；召回不推进消息上报游标。透明 `approval` 在协调器契约未核验时明确报错。非对话资源动态透传给原始 client；`client.native` 可取得原对象，直接从 `native` 发出的调用不会被 CoSens 捕获。原生 `with_options()`/`copy()` 返回仍带 CoSens 包装的 client。无法安全归一化的生成请求保持原生调用，并设置 `client.cosens.rsi_capture="unsupported"`。

基础接入只需 `native`、`api_key`、`mode`。决策默认复用本次原生请求的 `model`，因此不额外指定决策模型；它会通过原客户端的 Responses API 发起一次结构化判断请求，增加一次模型调用费用和延迟。该模型/服务须支持 Responses 结构化输出；分类失败仍遵循上述独立召回规则。`decision_model` 保留为高级可选覆盖项。未指定决策模型且请求中也无 `model` 时，SDK 在网络请求前报明确配置错误。

`session` 也是高级可选项：不传时仍捕获当前请求中可见的用户文本、模型回复和已适配的工具事件；但不能读取应用未传给 SDK 的历史。可用 `client.cosens.scope(session_id="...", turn_id="...")` 为一个逻辑 turn 指定边界。只有传入可读取的 Session，SDK 才能补发外部历史消息；Session 需提供稳定的 `session_id` 和 `get_items()`。建议应用在模型调用前将本次用户消息写入 Session，并保持稳定消息 ID；若调用后才以新 ID 写入此前由 SDK 合成的可见事件，当前通用 Session 协议无法证明两者是同一消息。同步 OpenAI client 与异步 `AsyncOpenAI` 均受支持。

## 方式二：显式执行 Agent

```python
import os
from agents import Agent
from cosens import cosens

agent = Agent(name="assistant", model="your-app-model", tools=tools)
client = cosens(agent, api_key=os.environ["RSI_API_KEY"], mode="disable")
result = await client.run(agent, "用户任务")
print(result.final_output)
print(client.cosens.get_turn(client.cosens.last_turn_id))
await client.aclose()
```

这里的 `disable` 关闭自动决策；显式 `run()` 本身强制执行 RSI 原子编排。原生 Agent、工具和 handoff 仍由 Agents SDK 在本地执行。还提供 `run_sync()`、`run_streamed()`。原生工具审批中断时不提交 RSI 结果；通过 `await client.pending_state(agent, turn_id)` 取得原生 `RunState`，按 Agents SDK 的 `approve()`/`reject()` 处理后调用 `await client.resume(agent, turn_id, native_state, session=session)`，继续同一 RSI Run。`input_artifacts=[{"name": "...", "content": "..."}]` 上传确实存在的初始文本材料；原生 `final_output` 若为含 `files` 列表的字典，会上传实际输出文本文件。完成后的真实反馈和改进可根据 `get_turn()` 返回的 `run_id`，调用下述原子接口；没有反馈不会自动生成改进任务。

## 方式三：直接调用原子接口

```python
import os
from cosens import CoSensClient

async with CoSensClient(api_key=os.environ["RSI_API_KEY"]) as rsi:
    prepared = await rsi.atoms.prepare(objective="用户任务", key="stable-request-key")
    run = await rsi.atoms.get_run(prepared.run_id)
    # 使用 prepared.recipe/context 与 run.snapshot，在应用内执行任务。
    await rsi.atoms.submit(prepared.run_id, data={"summary": "实际结果", "outcome": "completed"})
```

`rsi.atoms` 提供 `artifact`、`prepare`、`get_run`、`session_events`、`initial_inputs`、`submit`、`feedback`、`improve`、`get_improvement`、`resume_improvement` 和有界 `wait_improvement`。原子调用方须在首次业务模型调用前完成 `prepare/get_run`，并在工具循环中复用结果；不创建 Run 时使用 `await rsi.atoms.recall_context(query="任务", task_family="general")`，同一 turn 复用返回结果。管理员发布须显式使用 `rsi.admin.atoms.publish`，仍携带同一 Friday 用户 Key，由网关及后端校验；SDK 不自动发布，也不持有内部服务令牌。

## 恢复与服务端边界

Session 事件只在收到 `durable_ack` 后推进本地游标。失败后可查 `client.cosens.get_turn(turn_id)`；原生执行已完成但提交回执不明时用 `client.cosens.reconcile(turn_id)` 对账，不重新执行模型或工具。原生执行结果不明时抛 `RSIRecoveryRequired`，由宿主确认实际执行情况。透明调用的重试及工具续接必须保持 turn 创建时的 `mode`；模式变化或旧记录缺少模式时，SDK 会在模型调用和远端写入前拒绝隐式恢复。请持久化 `state_path`；丢失本地状态后，当前服务端不能保证跨 Run 的消息全局去重。

网关 OpenAPI 当前标识为 1.0.0；以本次 curl 附件、网关实际返回和联调报告为准。审批协调器 `/v1/invocations` 仍未确认，透明 `approval` 命中时保持明确报错。单元测试使用模拟 HTTP；真实网关联调使用独立测试标识并写入合成测试数据，模型执行用本地替身验证编排，未调用真实模型或银行系统。详细时机与限制见 [设计文档](docs/2026-10-26-cosens-python-sdk-design.md)。

## curl 文档对应的新增原子能力

普通原子接口（`atoms` 异步，`sync_atoms` 提供新增接口的同步形式）：

| SDK 方法 | 网关 `/api` 后的路径 |
| --- | --- |
| `get_active_recipe(task_family=...)` | `GET /v1/recipes/active?task_family=...` |
| `memory_ingest(content=..., request_key=...)` | `POST /v1/memory/ingest` |
| `memory_recall(query=..., token_budget=1000)` | `POST /v1/memory/recall` |
| `get_memory_job(job_id)` | `GET /v1/memory/jobs/{job_id}` |
| `get_memory_experience(experience_id, revision=...)` | `GET /v1/memory/experiences/{experience_id}` |
| `get_memory()` | `GET /v1/memory` |
| `get_recipe_step(run_id, node_id)` | `GET /v1/runs/{run_id}/recipe/steps/{node_id}` |
| `guidance(run_id, data=...)` | `POST /v1/runs/{run_id}/guidance` |
| `checkpoint(run_id, data=...)` | `POST /v1/runs/{run_id}/checkpoint` |
| `get_decision(run_id, decision_id)` | `GET /v1/runs/{run_id}/decisions/{decision_id}` |
| `get_trace(run_id)` | `GET /v1/runs/{run_id}/trace` |

`admin.atoms` 新增显式调用：`bootstrap_bundle`、`evaluate_decision`、`evaluate_recipe`，对应附件的 Recipe 注册、决策评估、Recipe 评估接口。它们不会由透明包装自动触发；调用者须提交实际执行和评估证据。`feedback` 不等于经验已经 verified，异步写入需轮询 Job/经验版本确认。

`recall_context` 顺序读取 Recipe 和 Memory 后冻结本地结果，服务端未承诺两个独立读取拥有同一个事务快照。`disable` 使用 `general` 任务族；`auto` 未命中时使用分类结果的任务族。Recipe `not_found` 或 Memory 空列表是合法结果，HTTP 错误和缺失字段仍阻止业务模型调用。

## 复现网关联调

先由运行环境注入 `RSI_API_KEY`（没有设置时脚本隐藏输入 Key），在仓库根目录运行：

```bash
# 只读验证：健康状态和无 Run 召回
python python/examples/validate_gateway.py --output /tmp/cosens-readonly.json

# 会写入合成 Recipe、Memory、Run 和评估记录
python python/examples/validate_gateway.py --write \
  --transcript /path/to/FULL_CURL_TRANSCRIPT.md --output /tmp/cosens-workflow.json

# 真实网关 + 本地模型/Agent 替身；会创建测试 Run，需要 agents 可选依赖
python python/examples/validate_wrapped_gateway.py --output /tmp/cosens-wrapped.json
```
