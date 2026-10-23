# 文档导航

本目录只保留当前实现的使用和维护文档。HTTP、MCP 及内部 Memory 的调用契约集中在接口文档，避免多份设计稿分别描述同一个入口。

| 文档 | 用途 |
| --- | --- |
| [RSI_WORKFLOW_API.md](RSI_WORKFLOW_API.md) | 完整接口文档：领域对象、Quick Start、53 个 HTTP 操作、18 个 MCP 工具、状态/错误/幂等、经验及发布闭环 |
| [openapi.json](openapi.json) | 实现生成的 HTTP Schema 快照与 `x-mcp-tools` 工具输入 Schema |
| [RSI_SERVICE.md](RSI_SERVICE.md) | 项目结构、安装、首次启动与最小接入 |
| [OPERATIONS.md](OPERATIONS.md) | 环境变量、systemd、凭证、队列、备份、日志、追踪及故障处理 |
| [RUNNERS.md](RUNNERS.md) | 通用执行器、任务族 backend、验收标准、隔离和候选验证接入 |

Memory 独立 SDK 见 [packages/memory/README.md](../packages/memory/README.md)，结构化经验见 [experience-memory.md](../packages/memory/docs/experience-memory.md)。官网构建、产品内容和发布约定见[仓库 README](../README.md)；实际网站内容位于 `dist/`，不受本目录清理影响。

旧的按日期记录、设计提案、验收流水、源码审计和迁移过程记录已从当前目录移除，仍可通过 Git 历史查看。历史设计不作为已实现接口的承诺；没有复制评测工程、缓存或原始运行数据到文档目录。
