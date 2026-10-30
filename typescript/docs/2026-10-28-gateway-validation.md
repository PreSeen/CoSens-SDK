# TypeScript SDK 国内网关联调记录

日期：2026-10-28（Asia/Shanghai）。区域：`cn`，网关前缀 `https://friday-rsi-cn.stardust-ai.com/api`，当前 `/openapi.json` 标识 `1.0.0`。测试使用 Friday 用户 Key；脚本逐请求断言 `X-Auth-Type: api_key`、`Authorization: Bearer <api_key>`，且未发送 `X-User-Id` 或 `X_User_Id`。报告将 Bearer 值固定脱敏，不保存 Key。

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 就绪、OpenAPI、独立 Recipe/Memory 召回 | 4 次 HTTP 200；`recallContext` 无 Run | [只读请求记录](testing/2026-10-28-cn-readonly.json) |
| 透明 `disable`、`auto` 未命中、`auto` 命中、显式 Agent | 11 次 HTTP 200；前两者无 Run，后两者各完成一个合成 Run | [包装入口请求及正文记录](testing/2026-10-28-cn-wrappers.json) |
| 原子 Artifact → prepare → getRun → sessionEvents → submit → trace | 8 次 HTTP 200；prepare 同 key 返回同 Run，重复事件 ACK 后 trace 每个 ID 只有一份 | [原子流程请求及正文记录](testing/2026-10-28-cn-atomic.json) |

包装入口测试使用锁定版 `openai@7.45.0` 的真实客户端对象，但模型请求由进程内假 `fetch` 返回；显式 Agent 的原生 Runner 也由本地替身执行。它们验证 SDK 与真实网关的编排、鉴权、上下文注入和提交契约，不验证真实模型质量。测试标识和 Run ID 见 JSON；合成 Artifact 与 Run 保留在远端。

复现时在 `typescript/` 设置有效 `RSI_API_KEY` 和 `COSENS_REGION=cn`，运行：

```bash
node --import tsx examples/04_validate_gateway_readonly.ts --output docs/testing/cn-readonly.json
node --import tsx examples/05_validate_wrappers.ts --write --output docs/testing/cn-wrappers.json
node --import tsx examples/06_validate_atomic.ts --write --output docs/testing/cn-atomic.json
```

后两个命令会写入独立命名的合成 Run。US 区域尚无有效 Key 的同等联调；本次同一环境对 US `/api/health/ready` 返回 401，见 [US 鉴权请求记录](../../python/docs/testing/2026-10-28-us-auth-recheck.json)，不能据此判断 US 的服务能力。审批协调器、Memory ingest/改进、管理员发布和 OpenAI 所有公开资源的逐方法运行时兼容矩阵不在这次真实网关验证范围内。
