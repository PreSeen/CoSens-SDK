# RSI 服务运维

调用字段与状态见 [接口文档](RSI_WORKFLOW_API.md)，执行器协议见 [RUNNERS.md](RUNNERS.md)。本文描述操作方法，不声明读取文档的机器已经安装、重启或启用了任何服务。

## 环境配置

`.env.example` 是模板。启动脚本读取私有 `var/local.env`；systemd 使用相同 EnvironmentFile。修改环境变量后应由运维选择合适窗口重启对应进程。

| 变量 | 默认/要求 | 用途 |
| --- | --- | --- |
| `COSENS_RSI_TOKEN` | 脚本生成随机值 | 普通 HTTP/MCP 凭证；勿使用代码中的开发回退值部署 |
| `COSENS_RSI_ADMIN_TOKEN` | 脚本生成随机值 | 批准、可信评估、初始化、发布、写回恢复 |
| `COSENS_MEMORY_INTERNAL_TOKEN` | 内部适配器启动必须提供 | 仅 RSI 服务持有的 Memory 凭证 |
| `COSENS_RSI_DB` | `./var/cosens-rsi.sqlite3` | RSI SQLite 文件 |
| `MEMORY_DB` | `./var/memory.sqlite3` | Memory SQLite 文件；可指向原有文件名 |
| `COSENS_MEMORY_URL` | `http://127.0.0.1:18104/memory` | RSI 到内部适配器的地址 |
| `COSENS_MEMORY_PROJECT` | `default` | 内部 Memory 项目 |
| `COSENS_MEMORY_TENANT` / `COSENS_MEMORY_PRINCIPAL` | `local` / `cosens-rsi` | 内部 SDK 主体，由部署者控制 |
| `COSENS_RSI_RUNTIME_TRIGGER` | `manual` | auto/approval/manual |
| `COSENS_RSI_IMPROVEMENT_TRIGGER` | `manual` | auto/approval/manual，与运行触发独立 |
| `COSENS_RSI_TRIGGER_PROMPT` | `config/trigger-classifier-prompt.md` | 触发判断提示词 |
| `COSENS_RSI_TRIGGER_CLASSIFIER_URL` / `COSENS_RSI_TRIGGER_CLASSIFIER_KEY` | 未设置 | 可选 HTTP 分类器地址及 Bearer |
| `COSENS_RSI_AUTO_PATTERNS` | 代码中的开发关键词规则 | 用 `;;` 分隔的正则；无分类器时的开发匹配 |
| `COSENS_RSI_EXPERIENCE_MODE` | `advisory` | off/shadow/advisory |
| `COSENS_RSI_EXPERIENCE_BETA` | 0.5，范围 0–2 | 经验优势权重 |
| `COSENS_RSI_EXPERIENCE_SUPPORT_PRIOR` | 3，范围 1–1000 | 独立任务支持度收缩 |
| `COSENS_RSI_EXPERIENCE_COST_WEIGHT` | 0.1，范围 0–1 | 候选归一化成本权重 |
| `COSENS_RSI_RECIPE_MEMORY_ENABLED` | `0` | `1` 启用 Recipe 经验生命周期 |
| `COSENS_RSI_GENERIC_RUNTIME` / `COSENS_RSI_GENERIC_CONFIG` | 未设置 | 通用执行器工厂及其配置文件 |
| `COSENS_RSI_HARNESS_BACKEND` / `COSENS_RSI_HARNESS_CONFIG` | 未设置 | 按任务族配置的后端工厂及其配置文件 |

经验策略及水位在 Run 准备时冻结，修改配置不会重写旧 Run。off 也不等于整个 Memory 服务离线：准备和知识能力仍有独立依赖。

runtime=approval 时，prepare 只生成限时批准请求，管理员消费后创建 Run。improvement=approval 目前只返回 approval_required，没有完整的改进批准消费接口；需要学习时应由运维配置受控的显式 manual 或 auto 流程。

## 启动、服务管理与入口

前台运行见 [安装说明](RSI_SERVICE.md)。安装 systemd 后：

```bash
systemctl --user status cosens-rsi.service
systemctl --user restart cosens-rsi.service
systemctl --user stop cosens-rsi.service
journalctl --user -u cosens-rsi.service -n 100
tail -n 100 var/rsi.log
```

安装器从 `deploy/cosens-rsi.service` 生成用户级服务文件，执行 daemon-reload、enable、restart 并检查 readiness。此命令确实会切换/重启服务，应只在获准部署时执行。

默认 API、dashboard、MCP 和 Memory 子应用共用 18104；脚本用 loopback 地址进行内部调用。若自定义监听地址/端口，应同时配置 CLI、内部 URL、MCP 客户端和反向代理；现有启动脚本的 18104 假设不能只靠一个变量自动全部替换。

`/health/live` 只证明进程响应；`/health/ready` 需要普通凭证并检查 Memory。未配置全局执行器不会必然令 readiness 失败，应检查 harness_improvement.generic_runtime 与作业等待原因。

## 数据、备份与迁移

RSI 数据库保存 Run、Recipe、产物、反馈、会话证据、作业、发布和投递状态；Memory 数据库保存知识与版本化经验。实验目录位于 RSI 数据库同级的 `harness-experiments/`，下载候选与恢复审计需要相应文件。

SQLite 可启用 WAL，直接复制单个活跃 `.sqlite3` 文件不是可靠的一致备份。使用 SQLite backup API 或经维护窗口停止写入再备份，并同时保存对应实验文件和配置版本。两个数据库没有跨库全局事务，需要保留 outbox/回执供恢复；敏感凭证应单独安全备份。

例：在维护流程中对单个数据库制作一致快照，路径由操作者明确指定：

```python
import sqlite3
with sqlite3.connect("var/cosens-rsi.sqlite3") as source:
    with sqlite3.connect("/secure-backup/cosens-rsi.sqlite3") as target:
        source.backup(target)
```

恢复前应停止相关写入，匹配两个库、实验目录和配置；不要重新运行种子初始化来“恢复”已有 active Recipe。包名统一没有改写现有数据库表名或图标签，也没有自动搬迁旧库。

## 经验队列与作业恢复

API lifespan 启动 Memory worker、决策/Recipe outbox dispatcher 和学习 worker。只启动 stdio sidecar 不会启动这些 API 后台线程。

| 现象 | 处理 |
| --- | --- |
| 决策 writes 长期非 done | 检查内部 URL/凭证、Memory 健康和日志；按 decision 查看 attempts/last_error |
| 决策 outbox failed | 检查失败原因，再用管理员 retry 接口；不要循环重放权限/版本错误 |
| Recipe 学习等待持久经验 | 检查 Recipe outbox 与日志；当前没有与决策队列相同的公开管理列表/重试接口 |
| awaiting_input | 上传真正的初始材料并补充 initial-inputs；已冻结时创建新 Run |
| awaiting_runner | 提供有版本、真实隔离的执行器配置后恢复等待作业 |
| awaiting_judge | 检查明确的验收声明或独立标准编译/审核能力 |
| awaiting_validation_tasks | 补充合适且未污染的独立任务，使用同一作业候选继续 |
| running 进程中断后未恢复 | 先核查外部调用和产物；当前不自动接管不确定调用，resume 也不允许重放 running |
| 发布拒绝 | 检查 validated 状态、active revision、基线、冻结报告、来源有效性和经验投递 |

当前是本地 SQLite 编排，不是多实例 PostgreSQL 队列、分布式故障接管或自动发布/回滚服务。

## Dashboard 与追踪

dashboard 按 Case 展示 Run、输入输出、阶段、经验建议、写回和 Recipe 演化。相同身份、项目、任务族下用 environment.case_id 分组；需要经验可比性时保持环境和合同一致。效果卡缺少可靠可比证据时不会承诺提升。

页面 HTML 无需认证，数据需要普通令牌，前端将该令牌保存在 localStorage。共享浏览器应由使用者管理凭证和清理。HTML 工件预览使用受限沙箱；数据库仍可保存原始工件正文。

导入已获授权的 CLI JSONL 或显式启动带追踪任务：

```bash
uv run python scripts/import-codex-trace.py /absolute/path/to/events.jsonl
uv run python scripts/run-codex-traced.py --name task --prompt-file /absolute/path/to/task.md
```

带追踪启动器会执行任务，不是只读工具。原始捕获与 dashboard 投影分开：稳定事件 ID 去重，完整换行记录获得 durable ACK 后推进 byte cursor；大事件可分块保存为私有 Artifact。捕获缺口不能补造成完整动作遥测，不采集模型内部推理。

所有 var 文件、令牌、数据库、原始日志和实验产物均不应加入 Git。通过反向代理暴露服务时，按部署授权保护管理和私有 Memory 路径；当前应用没有账户登录、CORS 白名单或统一限流产品能力。
