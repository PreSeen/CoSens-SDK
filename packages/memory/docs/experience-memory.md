# 结构化经验记忆

`ExperienceService(MemoryService(store))` 与现有知识记忆共享 Store 和项目权限，使用独立表保存结构化经历。调用前执行 Store.migrate()；已有数据库可原地增加这三张表：experiences、experience_versions、experience_receipts。SQLite 已有回归测试；PostgreSQL 使用同一兼容 DDL，但本轮没有真实 PostgreSQL 验证。

记录包含 run_id、task_family、phase、state、environment、contract_hash、action_schema、reward_spec、candidates、actual_action_id，可附 next_state、task_lineage_id、source_refs、策略与 Recipe 版本。动作包含 id、version、description；confidence/cost 是 0..1 的有限数。首次写入强制 pending，不接受客户端自证成功。

SDK 操作：

- upsert：幂等写入，支持 expected_revision；修改原始经历会使奖励重新 pending。
- append_outcome：可信 host administrator 才能追加 verified/inconclusive/retracted。需要 evaluation_id、reward_spec 和 evidence_ref；verified return 是 [-1,1] 内有限数。Memory 不自行判断外部评估报告正确与否。
- get：读取当前或指定版本；仍检查当前项目/记录 ACL 及来源有效性。
- search：先限定任务族、阶段、环境、动作契约、奖励规范、分区和水位，再做状态文本匹配，返回经验原始回报与引用。普通默认仅 production/train verified。扫描上限 2000，返回 scan_truncated；无向量或 event_focus 性能声明。
- watermark：获得项目单调水位。查询使用当前版本，纠正后的经验不会借旧水位恢复成旧奖励。
- set_readers / withdraw：记录级权限更新或撤回；撤回后历史版本也不可读。

经验不写入知识 sources/chunks/assertions/Wiki，也不启动 LLM compiler。普通 recall/read_wiki 不会召回经验或评测轨迹。Memory 来源引用在写入、读取和检索时校验当前 ACL/revision/hash/span；对应来源失效后，该经验不再供策略使用。

CoSens 的私有适配器 cosens_api.py 提供：

```text
GET  /internal/v1/experiences/watermark
POST /internal/v1/experiences/upsert
POST /internal/v1/experiences/search
GET  /internal/v1/experiences/{id}?revision=N
POST /internal/v1/experiences/{id}/outcomes
POST /internal/v1/experiences/{id}/withdraw
```

所有接口检查独立 Memory 内部 Bearer，项目/主体由服务配置构造。search HTTP 接口不接受 partitions 参数，不能扩大到评测分区。没有注册 Memory MCP 工具。在组合服务中这些路径加 `/memory` 前缀，与 RSI 共用 18104。

RSI 决定什么时候保存经历、怎样验证奖励、怎样计算动作优势；Memory 负责持久化、幂等、权限、版本和检索。RSI 中的 run trace 和评估报告仍是运行审计，不构成另一份可检索长期经验库。

测试入口为 tests/test_experiences.py；完整 RSI + Memory + HTTP/stdout MCP 联调位于 CoSens/tests/test_experience_http.py。

完整 HTTP 与 MCP 契约见 [RSI Workflow API](../../../docs/RSI_WORKFLOW_API.md)，运维和执行器配置分别见 [OPERATIONS](../../../docs/OPERATIONS.md) 与 [RUNNERS](../../../docs/RUNNERS.md)。
