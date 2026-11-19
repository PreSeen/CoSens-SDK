# RSI SDK V2 本地验证记录

分支：`RSI_SDK_v2`。基于原工作区 HEAD `1baf0ef` 新建分支；不合并原有未提交修改。

## 验证结果

从 Git 暂存区导出独立快照，使用该快照的源码和测试运行，避免依赖原工作区未提交内容。

| 环境 / 检查 | 结果 |
|---|---|
| Python 3.12：快照完整 Python 测试 | **176 passed, 3 skipped** |
| Python 3.10：新版本地 RSI 测试 | **18 passed** |
| Ruff：新增 Python 模块、示例和测试 | 通过 |
| Wheel / sdist 构建和公开包内容回归 | 通过 |
| Git diff whitespace 检查 | 通过 |

3 个跳过项是原有 PostgreSQL-backed HTTP 集成测试，需外部环境；新版本地流程没有使用该服务。原工作区额外存在的未提交测试不属于本次快照。

## 实际验证的路径

- 不可变文件快照、SHA-256、材料归档和创建幂等。
- 真实 LangGraph 图：分析 → Rubrics → Recipe → 文件任务执行 → 固定标准评分 → 迭代 / 结束。
- 文件产物确定性检查失败时，即使模型评分为满分也不能启用。
- 无测试任务、无法验证、必要问题未回答，均不能自动启用。
- 澄清携带问题原文及选项标签；执行中回答会使旧输入版本结果失效，完成后回答创建子 Execution。
- 答案已持久化但进程派发失败时，幂等重试会恢复派发，不重复创建子 Execution。
- 进程崩溃后回收过期租约并恢复检查点，不重复追加已提交版本；旧 Worker 被 fencing token 拒绝。
- 两次执行同时修订同一个 Recipe 时，只允许符合当前元数据修订号的执行自动更新启用指针。
- 学习图关闭继承自宿主的远端 LangSmith tracing；上游错误文本不持久化。

后台测试启动 **4 个独立 Hook 调用进程**；调用方退出后后台继续运行，完成真实工具调用、文件写入和评分。观测到同时运行 **2 个 Execution**，模型请求峰值 **1 个**，分别符合配置上限。所有进程共享同一个本地数据目录；不提供 CoSens API Key，也没有启动算法 HTTP 服务。

## 测试边界

模型端使用本机 OpenAI-compatible **协议测试服务器**，驱动真实 LangChain Agent 工具循环；另有确定性 Runtime 测试故障和并发条件。它们不是真实大模型，不证明真实模型的学习效果或吞吐量。

初版验收时尚未连接真实模型，未读取仓库 `.env` 或使用生产 API。后续真实模型联调见下节。

本版内置执行器支持受限文件任务，未提供 shell、浏览器和外部系统工具；复杂任务需扩展 Runtime。云端 Arq/HTTP/远端存储、Marketplace 同步和语义检索不在本次实现中。

安装和完整接口见 [本地 RSI 文档](../local-rsi.md)。

## 2026-11-18 真实模型联调与修复

用户要求继续测试后，使用仓库已有模型配置，对合成的 CSV 分组求和任务进行联调。仅发送合成上下文和测试文件，凭据只传入进程环境；未上传项目代码、真实业务材料或凭据。本机现有模型仅提供 completion 能力，不支持所需工具调用，因此联调使用已有远端网关；算法、SQLite、队列及后台进程仍全部在本地。

用历史输入及其确认产物学习，再要求 Recipe 处理不同金额的测试输入，实际写出 `summary.csv`，并检查排序、两位小数和完整文件内容。

实际发现：

1. 模型在工具参数中返回不合法 JSON，LangChain 结束时没有 `structured_response`，原实现直接校验 `None` 导致失败。
2. 修复后的第二次真实流程及诊断请求被测试网关以 **HTTP 402** 拒绝；网关明确报告余额不足。没有继续请求，也没有将失败伪装成测试通过。

修复内容：

- 保留已读取的证据和工具消息，在没有结构化结果时最多追加两次纠正；补齐错误工具调用对应的 ToolMessage，保持消息协议完整。
- 纠正仍失败时返回 `invalid_model_output`，保留阶段轨迹，不创建假 Recipe。
- 明确信息充分时 `hypothesis=null`、`questions=[]`，避免把 Recipe 总结当作 Hypothesis。
- 安全记录 HTTP 状态及 `model_payment_required/model_rate_limited/model_unavailable`，不保存上游响应正文、请求头或凭据。

新增回归覆盖：错误工具 JSON 恢复、普通文本替代结构化结果的恢复、超过纠正上限后明确失败，以及 HTTP 402 诊断与敏感响应不落盘。

修复后干净快照完整 Python 回归：**180 passed, 3 skipped**。工作区完整回归：**186 passed, 3 skipped**，另含原有未提交的 6 个测试。Python 3.10 新版本地 RSI 测试：**22 passed**。4 个独立 Hook 的后台并发测试再次通过，Execution 峰值为 2，模型请求峰值为 1。

**真实模型验收尚未完成，真实模型案例没有生成 Recipe。** 需要恢复可用模型服务后继续验证；协议测试和故障回归生成的测试 Recipe 不计为真实模型成功产物。

## 2026-11-18 指定模型复测：成功

上述失败属于旧配置下的联调记录。随后按用户明确指定的 `deepseek-v4.1-flash-1017` 与 `/v1/chat/completions` 重新执行真实模型案例，未修改仓库 `.env`，密钥仅传入测试进程环境。

首次发现模型把 `material_id` 填成了 `material_id/file-index`。已在材料清单中分别提供两字段，并在 EvidenceRef Schema 中添加格式与归属校验，使模型在结构化输出阶段获得纠错机会，而不是在保存 Rubrics 时才失败。补充了此类错误的真实 LangChain 工具循环回归。

同时补齐独立评价输入：评价 Agent 可以读取实际执行工具轨迹，并看到本次采用的 Recipe 内容，从而检查“实际读取了哪个输入”和“方法是否可复用”等标准；其对话上下文仍与测试执行 Agent 分离。

最终实测：

- Execution：`execution_b426cd9b0df14efab3e1585426addc48`。
- 约 **44 秒**完成：分析材料 → 创建 Rubrics → 生成 Recipe → 新输入实际执行 → 独立评分 → 自动启用。
- Recipe：`recipe_68aa77cc927d4b618b96c44713c27f98`，版本 `version_de52c4d551b5408db5af296ff0f1134e`。
- 首轮测试 `passed`；8 条 Rubrics 得分均为 1.0；完整文件 `text_equals` 检查通过。
- `provisional=false`、`activated=true`，没有生成必要澄清问题。
- 使用新输入计算得到 `book=1.25`、`pen=4.00`，没有复制历史产物中的 `book=20.00`、`pen=3.00`。
- 测试目录中的文件与 SQLite 检查点未发现凭据格式内容；未发布或同步到云端。

生成的实际产物：

```csv
category,total
book,1.25
pen,4.00
```

修复后干净快照 Python 回归 **181 passed, 3 skipped**，工作区回归 **187 passed, 3 skipped**；Python 3.10 新版模块测试 **23 passed**。这个结果验证了该 CSV 案例的真实模型流程，不代表所有任务的 Recipe 质量或模型吞吐量。
