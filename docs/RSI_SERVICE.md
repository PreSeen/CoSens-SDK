# RSI 服务安装与接入

本仓库同时包含 CoSens 官网、RSI 服务和独立 Memory SDK。服务是单机 SQLite 版本，支持 HTTP、HTTP MCP 与本机 stdio MCP。完整调用契约见 [RSI_WORKFLOW_API.md](RSI_WORKFLOW_API.md)。

## 代码布局

| 位置 | 内容 |
| --- | --- |
| `src/cosens_rsi/` | Run、Recipe、经验编排、改进作业、追踪与 dashboard |
| `packages/memory/src/memory/` | 来源、知识/Wiki、检索、版本化经验及私有适配器 |
| `tests/`、`packages/memory/tests/` | RSI、网站及 Memory 功能测试 |
| `scripts/`、`deploy/` | 启动、安装、MCP 包装器、执行器示例和 systemd 模板 |
| `plugins/cosens-rsi/`、`.agents/plugins/` | 可选本地 Codex 插件 |
| `business_cases/` | 案例源码与种子；生成数据和私有运行产物不随仓库发布 |
| `var/` | 本机凭证、数据库、日志和实验文件，Git 忽略 |

## 安装与首次启动

完整服务需要 Python 3.12+、uv。下面的命令在仓库根目录执行：

```bash
uv sync --frozen
scripts/run-local.sh
scripts/start-local.sh
```

`run-local.sh` 在不存在时生成三种随机本地凭证，保存在 `var/local.env`，权限为 0600。`start-local.sh` 运行前台服务，默认监听 `0.0.0.0:18104`，Ctrl-C 停止。本机访问：

- `http://127.0.0.1:18104/dashboard`：输入普通 RSI token 查看任务。
- `http://127.0.0.1:18104/docs`：主应用自动接口页。
- `http://127.0.0.1:18104/mcp`：HTTP MCP，需要普通 RSI Bearer。

已有相同地址的健康服务时，启动脚本会报告已运行；不要把此脚本用作隔离测试或连接任意现有实例的探测器。迁移安装应显式设置 `MEMORY_DB` 和 `COSENS_RSI_DB`，保留原数据库文件；包名调整不需要重建数据库。

```bash
set -a
source var/local.env
set +a
curl --fail-with-body -sS http://127.0.0.1:18104/health/ready \
  -H "Authorization: Bearer $COSENS_RSI_TOKEN"
```

## 运行方式与 Recipe

assist 由客户端自行执行，RSI 提供冻结上下文、进度与经验建议。guided 按冻结 DAG 发放步骤并校验输出；普通 Run 的 managed 模式尚不可用。

没有 active Recipe 时 assist 仍可准备任务。只有运维明确需要通用三步种子时才执行管理员 bootstrap；种子状态为 `active_seed_unvalidated`，不代表已验证的方法。bootstrap 会改变 active，不能作为普通健康检查。

知识与经验共用 Memory 内核，但普通 Wiki/recall 不混入结构化经验。RSI 的知识检索当前使用私有适配器的开发配置；合仓没有改变 Memory 的检索算法或默认参数。

## 可选持久服务和本机 MCP

安装 systemd 服务、修改客户端配置是显式运维动作，不由 `uv sync` 自动执行：

```bash
scripts/install-local-service.sh
uv run --with tomlkit python scripts/install-codex-local.py
```

前者安装并启动用户级 `cosens-rsi.service`；后者修改当前用户的 Codex 配置并安装本地插件，已有配置备份保存在私有 `var/`。本机 stdio wrapper 为 `scripts/cosens-rsi-mcp`，从其自身位置解析项目路径并加载本机凭证；它仍需要 Memory HTTP 服务在线。

服务配置、恢复与凭证处理见 [OPERATIONS.md](OPERATIONS.md)；后台学习执行器配置见 [RUNNERS.md](RUNNERS.md)。触发默认 manual，经验默认 advisory，Recipe Memory 默认关闭，配置安装不等于自动启用学习或发布。

## 功能检查

```bash
uv run --with pytest python -m pytest tests
uv run --project packages/memory --extra test python -m pytest packages/memory/tests
npm test
npm run build
```

HTTP/MCP 集成测试使用临时数据库及随机 loopback 端口。可选数据库测试在缺配置时跳过，不应把跳过视为真实外部数据库验收。

`scripts/smoke_full_workflow.py` 会向配置的服务写入测试 Run 和材料，须只针对准备好的测试实例运行。本文档整理不要求重新测量 Memory 指标或调用真实模型。
