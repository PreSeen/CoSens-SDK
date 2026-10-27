# CoSens SDK

为 OpenAI 应用接入 Recipe、Memory 召回与 RSI 经验沉淀。保留现有 OpenAI 调用方式，在每个 turn 执行前获取参考上下文，并按需将任务消息与结果提交到 CoSens 服务。

```python
import os
from openai import OpenAI
from cosens import cosens

native = OpenAI()  # 读取 OPENAI_API_KEY
client = cosens(native, api_key=os.environ["RSI_API_KEY"], mode="auto")
```

**[Python 接入文档](python/README.md) · [可运行示例](python/examples/) · [原子 API](python/docs/api-reference.md)**

## 能做什么

- **透明接入**：继续使用 `client.responses.create(...)` 等原生调用；其他 OpenAI 资源方法透传。
- **显式执行**：`await client.run(agent, user_input, session=session)` 组合原子步骤，由 OpenAI Agents SDK 执行任务。
- **原子调用**：自行编排召回、模型执行、消息上报和结果提交。
- **增量上报**：可读取的 Session 配合持久化 ACK 游标，已确认消息不会重复发送。
- **按 turn 召回**：`auto` 与 `disable` 都支持召回，同一 turn 的工具循环复用冻结结果。

## 快速开始

Python 3.10+。以下命令在本仓库根目录执行：

```bash
python3 -m venv python/.venv
source python/.venv/bin/activate
python -m pip install -e "./python[examples]"
cp python/.env.example python/.env
```

编辑 `python/.env`，填写 `OPENAI_API_KEY`、`OPENAI_MODEL`、`RSI_API_KEY`。然后在 IDE 选择 `python/.venv` 解释器，打开下面任一文件，点击 **Run**。不需要填写命令行参数，也不依赖 IDE 的工作目录。

| 场景 | 可运行文件 | 行为 |
| --- | --- | --- |
| 透明包装 | [01_transparent.py](python/examples/01_transparent.py) | 真实 OpenAI SDK；模型判断是否提交 RSI Run |
| 显式 run | [02_explicit_run.py](python/examples/02_explicit_run.py) | 真实 Agents SDK + OpenAI 模型；显式创建 Run |
| 原子编排 | [03_atomic.py](python/examples/03_atomic.py) | 展开任务准备、召回、模型调用、ACK、提交和 trace |

三个文件均访问真实服务，会产生模型调用及相应的服务端记录。运行前请填写可用凭证；自动化测试另使用模拟 HTTP。

## 语言支持

| SDK | 状态 |
| --- | --- |
| [Python](python/) | 0.1.0，支持从源码安装 |
| [TypeScript](typescript/) | 仅预留目录，尚未实现 |

CoSens 国内网关地址由 SDK 内部管理，无需传入 `base_url` 或 `X_User_Id`。OpenAI 模型凭证和 Friday Key 分别用于两个服务，具体配置、常见错误和恢复方式见 [Python 接入文档](python/README.md)。

## 开发与验证

```bash
python -m pip install -e "./python[examples,test]"
python -m pytest python/tests
python -m build python
```

- [设计与调用时机](python/docs/2026-10-26-cosens-python-sdk-design.md)
- [网关真实联调报告及范围](python/docs/2026-10-27-gateway-validation.md)
- [故障恢复与 API 参考](python/docs/api-reference.md)
