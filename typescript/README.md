# CoSens TypeScript SDK

Node.js 22 或更高版本。当前仓库锁定 `openai@7.45.0`、`@openai/agents@0.21.0`，以仓库内 `python/docs/2026-10-26-cosens-python-sdk-design.md` 第 8 节为实现与验收基线。TypeScript SDK 仍在开发中；国内网关的只读召回、包装入口和原子主链已用本地假模型联调，记录见仓库内 `typescript/docs/2026-10-28-gateway-validation.md`。

下游新 Task / Revision / Recipe Candidate 工作流的 TypeScript 原子方法与 Python 对照见[开发契约](../python/docs/2026-10-28-task-workflow-sdk-design.md)。本页下方的透明包装与 Runner 示例仍基于旧 `/v1/runs` 协议。

新版 Memory 与 guided Run 公共原子方法已实现：`memoryIngest({ content, requestKey, sourceId?, expectedRevision?, sourceKind?, durable? })`、`getMemoryItems({ limit? })`、`memoryRecall({ query, tokenBudget? })`、`prepare({ objective, key, executionMode: 'guided' })`、`nextStep(runId)`、`submitStep(runId, attemptId, { attemptToken, outputs, decisionId?, actualActionId? })`、`completeGuided(runId)`、`getEvents(runId)`。SDK 为兼容旧调用会显式发送 `sourceKind: 'decision'` 和 `durable: true`；原始服务省略字段时默认 `message` 和 `false`，需要该语义时请显式传入。Memory 列表 limit 范围 1–500；召回预算范围 1–8000。`managed` 执行模式尚不可用。guided 的 attempt 身份应持久保存；宿主步骤结果未知时先对账，不重放可能有副作用的动作。`completeGuided`、assist `submit` 及 Task 工作流是不同接口。

2026-10-29 固定 CN 区域后，Recipe、Memory 召回/列表、来源版本更新、guided Run 完整步骤流及 Task → Execution → Revision 冒烟测试通过，见 [验证记录](../python/docs/testing/2026-10-29-v3-cn-contract.json)。Memory 抽取作业仍 pending。一次性文档上传的 `/v1/document/upload-grants` 不在当前 CN OpenAPI，实际探测返回 404。SDK 已提前实现上传适配并完成离线测试，真实上传联调待服务部署。

IDEA 的 `00_all_tests_CN` 会运行 TypeScript 构建、类型检查、完整测试和 CN 网关检查；`00_all_tests_CN_live` 还会执行本目录三个真实模型示例。新工作流冒烟代码为 [05_task_workflow_cn_smoke.ts](examples/05_task_workflow_cn_smoke.ts)，由总测试配置运行。当前 CN 网关未部署新工作流路径时，冒烟脚本只读退出（代码 2）。

```bash
cd typescript
npm ci
npm run check
```

安装或打包本地 SDK 时，`prepare` 会编译 `dist/`；包内容只包含运行时代码、类型声明、README 和公开方法清单，不包含测试及联调记录。

配置好 Key 和 `OPENAI_MODEL` 后，可在本目录分别运行 `node --import tsx examples/01_transparent.ts`、`node --import tsx examples/02_explicit.ts`、`node --import tsx examples/03_atomic.ts`。三者访问真实服务；第三个入口会创建 Run，若模型已开始执行而流程中断，需先核对远端 Run 与本地结果，不能仅靠复用 `COSENS_TURN_ID` 重新执行模型。

运行环境提供 `RSI_API_KEY`（或 `COSENS_API_KEY`）；也可在构造选项中传 `apiKey`。模型服务单独使用 `OPENAI_API_KEY`。`COSENS_REGION=cn` 是默认值，可选 `us`。CoSens 服务端请求同时发送 `X-Auth-Type: api_key` 与 `Authorization: Bearer <api_key>`；这两个请求头不会加入原生 OpenAI 模型请求。`apiKey` 只填 Key 本身，不要包含 `Bearer `。

## 三种入口

以下示例假设应用已经安装本目录为本地依赖。透明代理保留原生 OpenAI 资源及请求选项；`client.native` 是原对象，直接调用它不会被 CoSens 采集。

```ts
import OpenAI from 'openai';
import { cosens } from 'cosens-sdk';

const native = new OpenAI();
const client = cosens(native, { mode: 'auto', statePath: './cosens-state.sqlite3' });
try {
  const response = await client.responses.create({ model: process.env.OPENAI_MODEL!, input: '用户任务' });
  console.log(response.output_text, client.cosens.lastTurnId);
} finally {
  await client.close(); // 只关闭 CoSens 拥有的状态资源；原生客户端由应用管理
}
```

`mode: 'disable'` 仍会在业务模型前独立召回 Recipe 与 Memory，但不会自动创建 Run。`auto` 先用原生模型做结构化分类，命中时创建 Run；`approval` 命中会明确报 `RSIApprovalUnavailable`，因为协调器尚无已验证契约。透明代理仅采集设计文档列出的生成方法；其他资源方法按原生语义透传。`client.withOptions(...)` 返回继续带有 CoSens 包装的派生客户端，复用同一状态文件与 Session 游标。

```ts
import { Agent } from '@openai/agents';
import { cosens } from 'cosens-sdk';

const agent = new Agent({ name: 'assistant', instructions: '完成用户任务' });
const runner = cosens(agent, { mode: 'disable', statePath: './cosens-state.sqlite3' });
try {
  const result = await runner.run(agent, '用户任务');
  console.log(result.finalOutput);
} finally {
  await runner.close();
}
```

显式 `run` 即使使用 `disable` 也创建 RSI Run。传入 Agents JS Session 时，Session 应提供稳定 ID 和 `getItems()`；跨进程恢复还必须由宿主持久化 Session 历史。`runStreamed()` 返回可异步迭代的原生结果包装；事件消费结束或等待 `stream.completed` 确认原生运行完成后提交。`toStream()` 和 `toTextStream()` 也保留原生入口。

```ts
import { CoSensClient } from 'cosens-sdk';

const rsi = new CoSensClient();
try {
  const context = await rsi.atoms.recallContext({ query: '用户任务' });
  console.log(context.recipe, context.snapshot.evidence);
} finally {
  await rsi.close();
}
```

原子入口只请求 CoSens，不执行模型。`atoms.prepare({objective,key})` 中的 `key` 是调用方生成并持久保存的幂等键；`admin.atoms` 只在调用者显式使用时发送管理请求。完整路径和请求体见设计文档第 8.2 节。

## 状态与恢复

Node 默认状态文件为 `~/.cosens/typescript-state.sqlite3`，可用 `statePath` 覆盖。SQLite 按区域、Key 不可逆标识、Session ID 和 Session incarnation 分隔记录；不会与 Python 的默认状态文件自动共享。生产环境应持久化此文件、Session 历史和相同的 Session ID。原生执行结果不明时不要重新调用模型或工具；先用 `client.cosens.getTurn(turnId)` 查看本地状态。`completion_pending` 和工具续接前的 `observability_pending` 可用 `await client.cosens.reconcile(turnId)` 对账；Agent runner 使用 `await runner.cosens.reconcile(turnId, { session })`。Responses 的响应 ID/会话引用与 Chat 的 `tool_call_id` 会将工具循环接回同一 turn；有 Run 时工具结果在下一次模型请求前上报并取得 ACK；Chat 流式 `delta.tool_calls` 会按索引组装后保留调用 ID。原生工具审批可通过 `pendingState` 和 `resume` 继续同一 Run：先调用 `const state = await runner.pendingState(agent, turnId)`，对 `state.getInterruptions()` 返回的待审批项调用 `state.approve(item)` 或 `state.reject(item)`，再调用 `runner.resume(agent, turnId, state)`。已上传 Artifact 的回执缺少 `artifact_id/content_hash` 或回执不明会报恢复错误，需要人工核对，不能盲传第二份；准备阶段重试必须沿用原输入 Artifact 清单。Agent 输出的 `finalOutput.files` 格式错误时会保留原生结果并进入 `completion_pending`，不会丢弃文件后继续提交。

锁定版 OpenAI 的 [399 个公开资源方法清单](tests/fixtures/openai-public-methods.json)已固定；393 个透传方法逐项验证参数、返回值和原生异常，六个采集入口另有运行时测试。使用锁定版原生客户端与进程内模型传输，另验证了文件二进制下载、文件、音频转录与 Uploads Part 的 multipart 上传、Batch 分页、Realtime SDP，以及 Embeddings、Moderations、Images、Vector Stores 和管理端 Projects 的实际请求与原生认证；Fine Tuning、Webhooks、Beta Agents、Evals、Containers、Skills、Videos、Audio Voice Consents 和 Conversations 也已验证只读请求。仍需覆盖其余公开资源方法的实际网络行为、透明模式的全部流生命周期分支和 US 网关；本版尚未完成生产验收。透明代理与显式 Agent 的同 Session SQLite 租约已经过双实例测试，租约重获也已用独立进程验证；跨进程恢复仍依赖宿主持久化 Session 历史。2026-10-28 CN 网关恢复后，TypeScript 的双鉴权头、独立召回、两个合成 Run 的包装入口及原子主链均返回 HTTP 200；当前 OpenAPI 仍标识 `1.0.0`。US 区域尚无有效 Key 的同等联调。

## 文档上传

`client.atoms` 提供 `createDocumentUploadGrant(metadata)`、`uploadDocumentToGrant(grant, content)`、`uploadDocument({ ...metadata, content })`、`getDocumentStatus(documentIds)`。内容为非空 `Uint8Array` 或 Node `Buffer`；普通上传使用 multipart，一次性上传使用原始字节 PUT。

```ts
import { readFile } from 'node:fs/promises';
import { CoSensClient } from 'cosens-sdk';

const client = new CoSensClient({ apiKey: process.env.RSI_API_KEY, region: 'cn' });
try {
  const content = await readFile('notes.txt');
  const grant = await client.atoms.createDocumentUploadGrant({
    filename: 'notes.txt', mimeType: 'text/plain', waitForProcessing: false,
  });
  const receipt = await client.atoms.uploadDocumentToGrant(grant, content);
  // 持久化回执并检查业务状态；HTTP 200 不等于文件已完成处理。
  if (receipt.document_id) {
    console.log(await client.atoms.getDocumentStatus([receipt.document_id]));
  }
} finally {
  await client.close();
}
```

普通 multipart 可单独调用 `await client.atoms.uploadDocument({ filename: 'notes.txt', mimeType: 'text/plain', content })`。不要在一次性上传结果不明时把普通上传作为自动回退。可选元数据为 `threadId`、`uploadedAt`、`sourceKind`、`workspaceId`、`ingestMode`、`summary`、`background`、`waitForProcessing` 和 `waitTimeoutSeconds`（整数 1–3600）。包导出 `DocumentUploadMetadata`、`DocumentUploadGrant` 类型；grant 的服务端字段保留 snake_case。

Key 可由构造参数 `apiKey` 或进程环境 `RSI_API_KEY` / `COSENS_API_KEY` 提供，SDK 本身不自动加载 `.env`。所有上传请求保留网关双鉴权头，只接受当前区域网关的指定上传路径，不跟随重定向，不转发任意 grant 请求头，不自动重试或重新签发。完整字段与恢复规则见[开发契约第 8.4 节](../python/docs/2026-10-28-task-workflow-sdk-design.md#84-文档上传)。CN grant 路径尚未部署，以上代码需待服务上线后联调；离线测试已纳入 IDEA `00_all_tests_CN`。

新增 30 个请求边界回归测试（Python / TypeScript 各 15 个），已纳入现有测试入口；用例清单、单独运行命令、缺陷修复与最新验证结果见[测试说明](../python/docs/testing/2026-10-29-sdk-request-regression.md)。
