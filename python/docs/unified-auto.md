# 统一 auto 流程（Python OpenAI 包装器）

默认 `cosens(native, mode="auto")` 保留原生模型调用和返回对象，在每个新业务 turn 执行：

1. 分类任务并识别针对已完成执行的反馈。
2. 查询当前用户的工作流 Recipe，检查 `candidate`、`current` 和任务适用条件；命中则冻结其 ID、版本及内容，同时召回 Memory。
3. 正常调用原生模型。需要记录的执行通过 Task / Execution 接口捕获输入、真实工具事件和输出，不走旧 Run 的自动 V1 提交流程。
4. 后台统一决定并执行 `create`、`revise` 或 `noop`。

| action | 触发条件 | 操作 |
| --- | --- | --- |
| `create` | 没有适用 Recipe，已记录的上下文足够提炼有证据的可复用方法 | 显式发起初始 Recipe 任务 |
| `revise` | 用户反馈关联某次执行所用的 Recipe，且需要改进 | feedback → attribution → 选择带证据的原因 → 新版本 |
| `noop` | 已有 Recipe 且无需改进、证据不足、无须记录的请求或重复生成任务 | 不发起 Recipe 生成/迭代请求；仍可复用已有 Recipe |

“有答案”或“聊天很长”不代表证据充分。判断读取任务目标、当前请求、可选 Agent Session、实际执行事件和结果；没有可复用方法或判断依据时返回 `noop`。没有真正反馈时不编造反馈。

## 原生对话输入

```python
from openai import OpenAI
from cosens import cosens

with cosens(OpenAI(), mode="auto", api_key=rsi_key) as client:
    messages = [{"role": "user", "content": task}]
    first = client.chat.completions.create(model=model, messages=messages)
    messages.append({"role": "assistant", "content": first.choices[0].message.content})
    # feedback_text 是用户实际输入。
    messages.append({"role": "user", "content": feedback_text})
    second = client.chat.completions.create(model=model, messages=messages)
    print(second.choices[0].message.content)
    print(client.cosens.get_auto())
```

Responses 同样支持原生关联：

```python
first = client.responses.create(model=model, input=task)
second = client.responses.create(
    model=model,
    previous_response_id=first.id,
    input=feedback_text,
)
```

SDK 使用明确的 response、conversation 或已记录的相邻 user/assistant 消息关联反馈，不使用全局“最后一个任务”猜测。历史被应用改写或缺少关联时，不自动归因到其他任务。每次执行的一次改进反馈冻结后，修改反馈文本需要针对下一次真实执行提交，避免在进行中的归因任务里偷偷替换输入。

## 结果与后台状态

```python
status = client.cosens.get_auto()  # 同步读取本地状态；AsyncOpenAI 下也一样
# action: create | revise | noop
# status: queued | running | completed | noop | paused | failed | awaiting_selection | blocked
# reason, turn_id, request_turn_id, used_recipe_id, used_recipe_version,
# recipe_candidate_id, error_type
```

`action` 是 Recipe 操作，`status` 是执行进度；queued 状态下 action 尚未最终决定。反馈调用的结果会关联被反馈的原执行。`completed` 仅说明候选 Recipe 已生成，不表示已验证、接受或发布。

生成与归因不阻塞原生回答；`get_learning(turn_id)` 可读取详细持久化记录。可选等待/恢复接口用于测试、批处理和运维，不要求用户聊天时等待：

```python
client.cosens.wait_learning(turn_id, timeout=60)
client.cosens.resume_learning(turn_id)
# AsyncOpenAI：await client.cosens.wait_learning(...)
#              await client.cosens.resume_learning(...)
```

应用应保持 client 存活以继续后台协调；关闭 client 会在安全边界停止本地轮询，服务端已启动的任务不会取消。重建 client 后可凭原 turn_id 恢复，SDK 不重新执行业务模型。SQLite 状态应使用持久化文件；`:memory:` 不能跨进程恢复。远端失败默认最多恢复一次相同 job ID；业务执行、输出 artifact、提交回执和原因选择的不确定状态均不得通过重跑模型解决。原因选择的请求结果未知且服务端不能查回子任务 ID 时会停止并报告失败。

同一次工具循环只召回一次，保持 Recipe 版本不变。若服务端选中的版本与冻结版本不同，SDK 在派发业务模型前报错；反馈迭代前也检查原执行所关联的版本，避免改到另一个版本。

检索当前最多查看一页 100 个摘要及其中 20 个 current 候选详情，再用当前模型检查 `when/when_not`、输入与工具能力。检索范围未覆盖全部候选且没有命中时，返回 `noop / recipe_search_incomplete`，不把有限检索误当成“完全没有 Recipe”。新生成的工作流候选完成后会在后续检索中参与复用；不会自动写入旧 Run 的 active Recipe 注册表。

## 配置与兼容

```python
from cosens import AutoLearning, cosens

client = cosens(native, mode="auto", auto_learning=AutoLearning(
    timeout=1800,
    poll_interval=2,
    max_job_retries=1,
    # 可选：显式配置服务端生成 Recipe 使用的模型。
    # model_api={"base_url": ..., "model": ..., "api_key": ...},
))
```

不会隐式把原生 OpenAI API Key 发送给 RSI 服务。`model_api` 只在显式配置时发送，且不写入本地学习状态。可选 `feedback(source)` 评估器必须返回真实纠正文本或 None；可选 `select_cause(portfolio)` 必须返回观察到的候选 ID 或 None。默认原因选择取首个带 evidence_refs 的候选，明确记录为自动假设，不冒充人工确认。

`auto_learning=False` 保留旧 Run 协议；`disable` 继续使用旧公开 active Recipe + Memory 召回，不运行本流程。此改动只覆盖 Python 的 OpenAI 包装器，Agent / Runner 包装器及 TypeScript 不会静默采用这个新协议。

## 必须配套的服务端契约

**服务端提交接口统一改为只记录执行结果，不再保留“提交即生成 V1”的旧行为。SDK 不发送生成开关字段。**

- `submit_execution(...)` 只记录结果和 Revision，不排队生成 V1。
- Recipe 只在 SDK 决定 `create` 或 `revise` 后通过相应独立原子接口创建。
- 请求与回执都不需要 `generate_recipe` 字段，不为旧调用保留自动生成分支。

配套补丁与契约测试：[backend/manual-recipe-generation.patch](backend/manual-recipe-generation.patch)、[backend/test_manual_recipe_generation.py](backend/test_manual_recipe_generation.py)。补丁直接移除部署代码中提交处理器的 V1 自动排队逻辑，不添加新参数；已在隔离副本验证，未修改部署 checkout 或上线服务。

如果尚未升级的服务端在提交回执中返回自动生成的 candidate ID，SDK 会保留原生回答并记录 `completion_pending / blocked`，避免误报 `noop` 成功。这只是识别服务端未按新职责拆分，不是兼容旧生成行为。必须先部署服务端变更，再恢复已有执行；不要通过再次执行模型来对账。
