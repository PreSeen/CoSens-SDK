# CoSens Python SDK

首版 Python SDK 实现三种入口：透明包装 OpenAI client、显式执行 Agents SDK Agent，以及直接调用 RSI 原子接口。当前服务端地址由 SDK 内部管理，公开初始化参数没有 CoSens `base_url`。

## 安装

```bash
pip install ./python
# 使用 Agents SDK 时：
pip install './python[agents]'
```

要求 Python 3.10+、`openai` 2.54.x；可选 Agents SDK 为 0.20.x。当前测试环境为 `openai==2.54.0`、`openai-agents==0.20.0`。通过 `COSENS_RSI_TOKEN` 提供普通 RSI Bearer；管理员发布另用 `COSENS_RSI_ADMIN_TOKEN`。SDK 持久化游标与恢复记录默认写在 `~/.cosens/state.sqlite3`，也可传 `state_path` 指定本地文件。

## 方式一：透明包装 OpenAI SDK

```python
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(native, mode="auto", decision_model="your-decision-model", session=session)
response = client.responses.create(model="your-app-model", input="用户任务")
print(response.output_text)

turn_id = client.cosens.last_turn_id
if turn_id:
    print(client.cosens.get_turn(turn_id))
client.close()
```

`mode="auto"` 先由独立模型决定是否启动 RSI；未命中、分类失败或 `mode="disable"` 时照常调用原生方法。`mode="approval"` 在当前服务端缺少 `/v1/invocations` 时会明确报错，不会假装完成审批。非对话资源动态透传给原始 client；`client.native` 可取得原对象，直接从 `native` 发出的调用不会被 CoSens 捕获。原生 `with_options()`/`copy()` 返回仍带 CoSens 包装的 client。无法安全归一化的生成请求保持原生调用，并设置 `client.cosens.rsi_capture="unsupported"`。

可用 `client.cosens.scope(session_id="...", turn_id="...")` 为一个逻辑 turn 指定边界。只有传入可读取的 Session，SDK 才能补发历史消息；Session 需提供稳定的 `session_id` 和 `get_items()`。同步 OpenAI client 与异步 `AsyncOpenAI` 均受支持。

## 方式二：显式执行 Agent

```python
from agents import Agent
from cosens import cosens

agent = Agent(name="assistant", model="your-app-model", tools=tools)
client = cosens(agent, mode="disable", session=session)
result = await client.run(agent, "用户任务", session=session)
print(result.final_output)
print(client.cosens.get_turn(client.cosens.last_turn_id))
await client.aclose()
```

这里的 `disable` 关闭自动决策；显式 `run()` 本身强制执行 RSI 原子编排。原生 Agent、工具和 handoff 仍由 Agents SDK 在本地执行。还提供 `run_sync()`、`run_streamed()`。原生工具审批中断时不提交 RSI 结果；通过 `await client.pending_state(agent, turn_id)` 取得原生 `RunState`，按 Agents SDK 的 `approve()`/`reject()` 处理后调用 `await client.resume(agent, turn_id, native_state, session=session)`，继续同一 RSI Run。`input_artifacts=[{"name": "...", "content": "..."}]` 上传确实存在的初始文本材料；原生 `final_output` 若为含 `files` 列表的字典，会上传实际输出文本文件。完成后的真实反馈和改进可根据 `get_turn()` 返回的 `run_id`，调用下述原子接口；没有反馈不会自动生成改进任务。

## 方式三：直接调用原子接口

```python
from cosens import CoSensClient

async with CoSensClient() as rsi:
    prepared = await rsi.atoms.prepare(objective="用户任务", key="stable-request-key")
    run = await rsi.atoms.get_run(prepared.run_id)
    # 使用 run 中冻结的 recipe/context/snapshot，在应用内执行任务。
    await rsi.atoms.submit(prepared.run_id, data={"summary": "实际结果", "outcome": "completed"})
```

`rsi.atoms` 提供 `artifact`、`prepare`、`get_run`、`session_events`、`initial_inputs`、`submit`、`feedback`、`improve`、`get_improvement`、`resume_improvement` 和有界 `wait_improvement`。管理员发布须显式使用 `rsi.admin.atoms.publish` 与独立管理员 token。

## 恢复与服务端边界

Session 事件只在收到 `durable_ack` 后推进本地游标。失败后可查 `client.cosens.get_turn(turn_id)`；原生执行已完成但提交回执不明时用 `client.cosens.reconcile(turn_id)` 对账，不重新执行模型或工具。原生执行结果不明时抛 `RSIRecoveryRequired`，由宿主确认实际执行情况。请持久化 `state_path`；丢失本地状态后，当前服务端不能保证跨 Run 的消息全局去重。

目前远端 OpenAPI 标识为 1.0.0，附件参考契约为 2.1.0。当前远端有 `session-events`，没有审批协调器 `/v1/invocations`。首次发布前需要在授权的测试环境验证写接口的完整响应和事件 ACK 行为。本仓库测试使用模拟 HTTP 传输，不向当前服务写入数据。详细时机与限制见 [设计文档](docs/2026-10-26-cosens-python-sdk-design.md)。
