# CoSens Python SDK

在现有 OpenAI 应用中加入 Recipe、Memory 召回，以及 RSI 任务消息与结果上报。业务任务由你的 OpenAI 客户端或 Agents SDK 执行，CoSens 负责上下文与经验流程。

[安装](#安装) · [配置](#配置) · [快速开始](#快速开始) · [三种可运行示例](#三种可运行示例) · [常见问题](#常见问题) · [API 参考](docs/api-reference.md)

Python 任务工作流原子方法见 [API 参考](docs/api-reference.md)。本页的透明包装及显式 Run 示例使用旧 `/v1/runs` 协议。

Memory、guided Run 和任务工作流原子方法支持 `client.atoms` 和 `client.sync_atoms`。文档上传使用 multipart；算法服务当前版本已移除一次性上传 grant。`TaskWorkflowRunner` 和 `CoSensClient.start()` 的原始实现及恢复契约缺失，调用会明确报错，任务工作流需使用原子方法由宿主编排。

真实网关上的新工作流路由及运行器状态需要在目标环境另行验证。

## 安装

要求 **Python 3.10+**。当前支持 `openai` 2.54.x，Agent 接入使用 `openai-agents` 0.20.x。以下命令均从 **CoSens-SDK 仓库根目录**执行：

```bash
python3 -m venv python/.venv
source python/.venv/bin/activate
python -m pip install -e "./python[examples]"
```

Windows 激活命令为 `python\.venv\Scripts\activate`。`examples` 安装三个示例所需的 Agents SDK 和 dotenv。仅使用透明包装可安装 `python -m pip install ./python`；仅增加 Agent 支持可安装 `"./python[agents]"`。这里使用源码安装，不依赖同名 PyPI 包是否已发布。

## 配置

复制 [.env.example](.env.example) 为同目录的 `python/.env`，填写：

```dotenv
OPENAI_API_KEY=your-openai-key
OPENAI_MODEL=your-model-name
RSI_API_KEY=your-friday-key
```

| 配置项 | 用途 | 必填 |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI 或兼容模型服务的凭证 | 是 |
| `OPENAI_MODEL` | 你有权限调用的模型名，示例使用 Responses API | 是 |
| `RSI_API_KEY` | 通用网关 Key；未设置区域 Key 时使用 | 与当前区域 Key 二选一 |
| `OPENAI_BASE_URL` | 自定义兼容模型服务地址；不填使用 OpenAI 默认地址 | 否 |
| `COSENS_MODE` | 透明示例的模式，默认 `auto`，可改为 `disable` | 否 |
| `COSENS_INPUT` | 示例任务文字，不填使用内置集成测试问题 | 否 |
| `COSENS_REGION` | CoSens 服务区域，`cn`（默认）或 `us` | 否 |
| `RSI_API_KEY_CN` / `RSI_API_KEY_US` | SDK 和示例按当前区域选用的网关 Key | 与 `RSI_API_KEY` 二选一 |

三个示例自动读取 `python/.env`，已有进程环境变量优先。**SDK 本身不自动读取 `.env`**；你自己的应用可使用环境变量或 `load_dotenv()`。

CoSens 网关由 SDK 内部按 `COSENS_REGION` 选择：国内 `cn` 为 `https://friday-rsi-cn.stardust-ai.com/api`，美国 `us` 为 `https://rsi.cosens.ai/api`。无需传入 CoSens `base_url` 或 `X_User_Id`，用户身份由网关注入。`OPENAI_BASE_URL` 仅配置模型服务。

### 国内区与美国区测试

三个示例共用同一套代码，在 `python/.env` 中配置区域与对应 Key：

```dotenv
COSENS_REGION=us
RSI_API_KEY_CN=your-cn-friday-key
RSI_API_KEY_US=your-us-friday-key
```

这些值是占位符，真实联调需要有效 Key。SDK 和示例优先使用对应区域的 Key，未设置或为空时回退到 `RSI_API_KEY`，再回退到 `COSENS_API_KEY`；示例显式填了占位符时会在发送请求前提示补全配置。不要假设两区 Key 通用。显式传入 `api_key` 的优先级最高。

IDEA 本地运行配置 `01_transparent`、`02_explicit_run`、`03_atomic` 固定测试国内区；同名加 `_US` 的三个入口测试美国区。也可直接打开示例运行，此时使用 `.env` 中的区域。终端临时切换示例：

```bash
COSENS_REGION=us python/.venv/bin/python python/examples/01_transparent.py
COSENS_REGION=us python/.venv/bin/python python/examples/02_explicit_run.py
COSENS_REGION=us python/.venv/bin/python python/examples/03_atomic.py
```

客户端在初始化时固定区域，切换区域需新建客户端。两区的 Run 恢复状态和消息确认游标相互隔离；国内区已有状态继续可用。

每个 CoSens 服务端请求携带 `X-Auth-Type: api_key` 和 `Authorization: Bearer <api_key>`。Friday Key 经 HTTPS 发送，证书验证保持开启，不跟随重定向；这两个头不会加到原生 OpenAI 模型请求。请使用环境变量或部署平台密钥管理；`.env` 已被 Git 忽略。不要把真实 Key 写入提交到仓库的源码。

## 快速开始

```python
import os
from openai import OpenAI
from cosens import cosens

native = OpenAI()
client = cosens(native, mode="auto")
try:
    response = client.responses.create(
        model=os.environ["OPENAI_MODEL"],
        input="请帮我总结这次任务中可以复用的经验。",
    )
    print(response.output_text)
finally:
    client.close()
```

接入只需 `native` 和 `mode`；也可显式传入 `api_key`。SDK 默认复用业务请求的模型做提交判断，无需额外填写 `decision_model` 或 `session`。网关 Key 的优先级为显式 `api_key` → 当前区域的 `RSI_API_KEY_CN` / `RSI_API_KEY_US` → `RSI_API_KEY` → `COSENS_API_KEY`。

## 三种可运行示例

在 PyCharm / IntelliJ Python 插件 / VS Code 中，把解释器选为本项目的 **`python/.venv/bin/python`**（Windows 为 `python\.venv\Scripts\python.exe`）。完成 `.env` 配置后，打开对应文件点击 **Run Python File / Run**。也可从仓库根目录运行下列命令。

### 1. 透明包装 OpenAI

**[打开 01_transparent.py](examples/01_transparent.py)**

```bash
python python/examples/01_transparent.py
```

核心调用仍为原生 OpenAI 形式，不显式调用 CoSens `run()`：

```python
client = cosens(native, api_key=friday_key, mode="auto")
response = client.responses.create(model=model, input=user_input)
print(response.output_text)
```

`auto` 先用模型判断是否需要 RSI Run；命中则准备 Run、获取冻结上下文，再执行业务模型、上报消息和提交结果。不命中或分类失败时，通过公开 Recipe + Memory 接口召回，不创建 Run。`disable` 跳过自动判断和自动 Run，但仍然召回。

运行会输出答案、capture、turn_id、run_id 和本地 state。**auto 没有创建 Run 是合法结果**；不要将 `run_id: None` 直接视为失败。`auto` 的决策请求增加一次模型调用，模型服务需支持 Responses 结构化输出。高级参数 `decision_model` 可覆盖默认选择。

### 2. 显式调用 run

**[打开 02_explicit_run.py](examples/02_explicit_run.py)**

```bash
python python/examples/02_explicit_run.py
```

```python
agent = Agent(name="assistant", model=model, tools=[])
client = cosens(agent, api_key=friday_key, mode="disable")
result = await client.run(agent, user_input, session=session)
print(result.final_output)
```

完整文件使用 `OpenAIResponsesModel` 绑定真实 `AsyncOpenAI`，因此你配置的模型 Key 和模型地址会被实际使用。`disable` 关闭自动触发，显式 `run()` 仍组合原子步骤完成 RSI 流程；不会再调用分类模型。

示例使用 Agents SDK 的 `SQLiteSession` 保存聊天历史，数据库位于 `~/.cosens/example-sessions.sqlite3`，控制台打印 session_id。每次点击生成一个新会话；连续聊天应复用同一个 Session。历史中已获得 `durable_ack` 的事件不重复上报。示例关闭 Agents tracing，避免额外发送 tracing 数据；可按应用需要自行开启。

### 3. 直接编排原子接口

**[打开 03_atomic.py](examples/03_atomic.py)**

```bash
python python/examples/03_atomic.py
```

完整文件展开以下主要流程：

```text
上传输入 artifact
  → prepare（processing 时以同一 key 有界轮询）
  → get_run，冻结 Recipe / Context / Snapshot
  → session_events，确认用户消息 ACK
  → 应用调用真实 OpenAI 模型，使用冻结上下文
  → session_events，确认助手回复 ACK
  → 上传输出 artifact → submit → get_trace
```

这里不调用包装器 `run()`，应用自己组织原子方法。服务端召回发生在模型调用前，工具循环应复用冻结结果。示例是单任务演示：每次运行生成新的 key；打印的 key、run_id 和实际输出须用于失败后的对账，不能直接重跑代替恢复。

有决策分支的任务可加入 `guidance` / `checkpoint`；只有拿到真实反馈后才调用 `feedback` / `improve`，发布需显式调用管理员原子接口。完整方法与调用时机见 [API 参考](docs/api-reference.md)。生产应用还需持久化原子流程自身的任务状态和 ACK 游标。

## 模式与兼容性

| 入口 / 模式 | 模型调用前召回 | 创建 RSI Run | 执行者 |
| --- | --- | --- | --- |
| 透明 `auto` | 每 turn 一次 | 由模型判断 | 原始 OpenAI client |
| 透明 `disable` | 每 turn 一次 | 不自动创建 | 原始 OpenAI client |
| 显式 `run()` + `disable` | 每 turn 一次 | 显式创建 | OpenAI Agents SDK |
| 原子接口 | 由应用在模型前调用 | 应用显式 prepare | 应用自己的模型/工具代码 |

当前透明 `approval` 协调器契约未确认，命中审批流程会明确报错；不要用于这些示例。

同步 `OpenAI`、异步 `AsyncOpenAI` 均可包装。对话生成适配 Responses / Chat Completions 的 create、parse、stream；文件、向量库等其他资源方法透传，原生返回值保持原形。`with_options()` / `copy()` 仍返回包装对象。对于无法安全捕获的生成请求，SDK 保留原生调用，设置 `rsi_capture="unsupported"`，此时不保证 RSI 召回与上报。直接调用 `client.native` 会绕过包装。

## Session 与故障恢复

不传 Session 的透明调用只能捕获本次请求可见的消息，无法读取应用未提供的旧聊天。外部历史、稳定 turn 边界和工具循环的进阶接入见 [Session 说明](docs/api-reference.md#session工具和恢复)。

CoSens 默认把状态和 ACK 游标写入 `~/.cosens/state.sqlite3`，可用高级参数 `state_path` 指定。记录按服务及凭证摘要隔离；部署时应持久化该文件。只有远端 ACK 确认后才推进消息游标，丢失本地状态后不能保证跨 Run 全局去重。

```python
turn_id = client.cosens.last_turn_id
state = client.cosens.get_turn(turn_id)
```

如果模型结果已经返回，但 state 为 `completion_pending`，不要直接重新执行模型或工具。保留原 Session、状态文件和 turn_id，透明同步客户端使用 `client.cosens.reconcile(turn_id)` 对账，异步 OpenAI 客户端使用 `await`。显式 Agent 示例把 Session 传给了 `run()`，恢复时也必须传入同一个 Session：

```python
# 重启后用控制台记录的原 session_id 和同一数据库重新创建 SQLiteSession。
await client.cosens.reconcile(turn_id, session=session)
```

原生执行结果不明或抛出 `RSIRecoveryRequired` 时，需要应用先核实实际执行情况。详见 [恢复边界](docs/api-reference.md#恢复与服务端边界)。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `ModuleNotFoundError: cosens / agents / dotenv` | 确认 IDE 使用安装过 `./python[examples]` 的解释器 |
| 提示缺少配置 | 将 `.env.example` 复制到 `python/.env`，填写三个必填值；检查 IDE 环境变量是否覆盖它 |
| 模型 401 / 403 / 404 | 核对 OpenAI Key、模型访问权限、模型名与模型服务地址 |
| 网关 401 / 403 | 核对 Friday 用户 Key 及网关权限，无需自己填写 X_User_Id |
| 召回失败、未调用业务模型 | 检查网关连通性与返回契约；SDK 不把错误静默当成空召回 |
| 模型有答案、RSI 未完成 | 保存 turn_id / run_id，按恢复流程处理，避免重复执行 |
| 想验证每次都创建 Run | 运行显式 run 或原子示例；auto 的创建与否由模型决策 |

示例错误只输出类型、HTTP 状态和 request_id，不输出远端错误正文。请结合上述信息和服务端日志定位。

## 开发与测试

```bash
python -m pip install -e "./python[examples,test]"
python -m pytest python/tests
python -m build python
```

`tests/test_runnable_examples.py` 从不同工作目录执行三个脚本，使用真实 OpenAI SDK、真实 Agents Runner 和本地状态存储，仅模拟 HTTP 服务，验证召回顺序、上下文注入、消息上报和凭证隔离。它不会消耗真实模型额度。

`examples/validate_gateway.py` 与 `examples/validate_wrapped_gateway.py` 是独立网关联调脚本，后者使用本地模型替身；它们与上述真实模型示例用途不同。复现方式、已验证范围见 [网关联调报告](docs/2026-10-27-gateway-validation.md) 和 [API 参考](docs/api-reference.md#复现网关联调)。

## 进一步阅读

- [设计文档与每步调用时机](docs/2026-10-26-cosens-python-sdk-design.md)
- [原子 API 与恢复边界](docs/api-reference.md)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)

新增 30 个请求边界回归测试（Python / TypeScript 各 15 个），已纳入现有测试入口；用例清单、单独运行命令、缺陷修复与最新验证结果见[测试说明](docs/testing/2026-10-29-sdk-request-regression.md)。
