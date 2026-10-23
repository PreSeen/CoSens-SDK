# RSI 执行器与改进流程接入

本文描述当前部署端扩展点。接口调用见 [RSI_WORKFLOW_API.md](RSI_WORKFLOW_API.md)，配置和恢复见 [OPERATIONS.md](OPERATIONS.md)。配置工厂属于受信任服务代码，不接受普通 HTTP/MCP 调用方提交任意命令或 Python 模块。

## 两种入口

| 入口 | 配置 | 适用方式 |
| --- | --- | --- |
| 通用执行器 | COSENS_RSI_GENERIC_RUNTIME=module:factory | 没有专用 backend 的任务族，从授权历史 Run、初始材料、事件和纠错编译任务包 |
| 任务族 backend | COSENS_RSI_HARNESS_BACKEND=module:factory | 部署者已有固定任务集、执行器和评审协议的任务族 |

没有任务族 backend 不意味着请求立即 deferred：它先进入通用准备，缺配置/材料/评审或独立任务时保存明确的 awaiting 状态。任务族 backend 若来源 Run 没有 Recipe，则可直接 deferred。

## 通用执行器

工厂返回 RuntimeProfile，字段包括 version、driver_factory、isolated、proposer、spec_builder、context_provider、policy、budget、train_rollouts。version 必须非空，driver_factory 可调用，train_rollouts 为 1–8；配置及各版本进入冻结合同。

```bash
export COSENS_RSI_GENERIC_RUNTIME=scripts.generic_runtime_example:build_runtime
export COSENS_RSI_GENERIC_CONFIG=/absolute/path/to/runtime-profile.json
```

受信任命令适配器示例配置：

```json
{
  "version":"agent-model-tools-v1",
  "kind":"command",
  "agent_argv":["/absolute/path/to/isolated-agent-wrapper"],
  "proposer_argv":["/absolute/path/to/isolated-proposer-wrapper"],
  "compiler_argv":["/absolute/path/to/independent-criterion-compiler"],
  "reviewer_argv":["/absolute/path/to/independent-criterion-reviewer"],
  "calibration_id":"operator-reviewed-control-suite-v1",
  "isolated":true,
  "learning_policy":{"max_rounds":1},
  "train_rollouts":2
}
```

isolated:true 必须对应运维实际实现的文件系统、网络、凭证及跨实验隔离，不是开启沙箱的开关。Agent 和角色子进程会过滤服务/Memory 配置前缀，但这不能替代操作系统隔离。

Agent wrapper 在临时工作目录运行，通过 stdin 接收 `{"instruction":"…"}`，stdout 返回一个 JSON 对象；日志写 stderr。返回 completed、outputs，以及可取得的真实 action_trace。text/artifact 输出按节点的 output_contract 组织，不依赖某个业务固定文件名。

proposer/标准 compiler/reviewer 的命令 wrapper 接收相应角色的 JSON payload 并返回 JSON 对象。compiler/reviewer 可省略，但此时仅明确结构化的验收声明能自动编译；使用 calibration_id 时必须同时配置独立 compiler 与 reviewer。

kind:codex 使用现有 CodexStepDriver/CodexJSONRole，可由配置指定 model/reasoning。它依赖部署者已有的客户端认证、权限和隔离环境；本接口不管理第三方模型账号，也不保证完整工具遥测。

源实现：[generic_runtime_example.py](../scripts/generic_runtime_example.py)、[RuntimeProfile](../src/cosens_rsi/runtime.py)。

## 任务族 backend

```bash
export COSENS_RSI_HARNESS_BACKEND=scripts.harness_backend_example:build_backends
export COSENS_RSI_HARNESS_CONFIG=/absolute/path/to/harness-config.json
```

工厂返回 `{task_family: LocalHarnessBackend(...)}`。示例 JSON 按任务族定义 agent_argv、driver_version、learning_policy、execution_budget、train_rollouts 和 cases。每个 case 提供 id、lineage_id、partition、task、inputs、snapshot、tests。

```json
{
  "reporting":{
    "agent_argv":["/absolute/path/to/agent-json-wrapper"],
    "driver_version":"model-tools-v1",
    "learning_policy":{"max_rounds":1,"min_confirm_tasks":4},
    "train_rollouts":2,
    "cases":[{
      "id":"training-example","lineage_id":"task-example","partition":"train",
      "task":"生成报告","inputs":{"input.txt":"已授权源材料"},
      "snapshot":{"evidence":["冻结证据"]},
      "tests":{"checks":[{"path":"report.json","kind":"exists"}]}
    }]
  }
}
```

此示例只展示一个训练任务格式，不能直接充当完整验证集。实际配置需补齐独立 train/screen/confirm 任务；exists 只检查存在，不证明业务质量。默认至少四个 confirm 是实现下限，不保证统计功效。

源实现：[harness_backend_example.py](../scripts/harness_backend_example.py)、[LocalHarnessBackend](../src/cosens_rsi/evaluation.py)。

## 任务材料、标准与轨迹

任务包包含原始目标、不可变初始 Artifact、Memory 快照、会话事件、纠错与可追溯作者。最终交付物不能替代缺失的初始材料；晚补输入明确标为 supplemented，执行材料冻结后需要新 Run。

通用标准编译支持 exists、contains、json_equal、json_field_equal、numeric_equal；明确 acceptance.checks 和反馈的 acceptance_checks 提供声明来源。模糊或矛盾标准会等待 judge。自然语言标准需独立 compiler/reviewer，在不接触本次候选与输出的情况下检查来源和覆盖。

calibration_id 记录运维完成的校准版本，不是系统自动证明评审正确。比较会核对完整 criterion 集合，不能以双方都漏报某个检查作为通过。

真实动作轨迹形状：

```json
{
  "schema":"action-trace-v1","complete":true,
  "events":[{
    "id":"read-1","sequence":1,"node":"produce","kind":"action",
    "action":"inspect-input","input_hash":"sha256:…",
    "state_before":"sha256:…","state_after":"sha256:…",
    "status":"passed","repair":false
  }]
}
```

kind 为 action/check，status 为 passed/failed，可附 error_kind=method/infrastructure。complete 是可信适配器的覆盖声明，Schema 校验不能证明没有遗漏；缺完整轨迹不能据此删节点或声称探索收益。基础设施失败和截断日志不得当成已确认的方法失败。

## 候选、独立验证与发布

新旧执行路径共用局部变更、DAG/端口校验和配对比较规则。通用持久化流程当前是一轮局部搜索；已有任务族 backend 保留其有界多轮机制。候选只在实验区保存，确认通过后仍需管理员显式发布。

接受规则包括独立质量提升，或在满足质量非劣效和关键成功条件后确认探索指标减少；未观察到显著下降不等于已证明非劣效。费用不是当前通用学习器的发布保证。

全局 exposure 记录任务内容与已知谱系，改任务族、Run 或作业名称不能清除暴露。训练/筛选/确认材料在给角色之前预占范围；默认实验 actor 不访问生产 Recall/Wiki，独立确认明细不回流候选生成。自定义 context_provider 必须落实允许来源与谱系。

材料不足时保存候选和阶段，后续 resume 继续等待作业；不自动重放未知的 running 外部调用。通用首次发布可从私有 seed 产生候选，发布前不修改 active；首次 expected_revision 为 null，已有生产版本要求匹配 revision 与原始基线。

## Recipe Memory

COSENS_RSI_RECIPE_MEMORY_ENABLED=1 时，真实评估观察和发布经验通过独立 outbox 写入 Memory。proposer 读取绑定实验的 train 视图，screen/confirm 不进入训练；新 Run 只读取匹配当前 bundle_hash 的 production release 经验。

该开关不把原始经验编译进 Wiki，也不改变现有候选动作的 Q/优势/支持度评分公式。发布要求相应经验已持久投递和训练视图已读取，缺失时拒绝发布。

接入检查可使用 tests/test_harness.py、tests/test_harness_http.py、tests/test_generic_improvement.py、tests/test_generic_http.py、tests/test_recipe_memory.py。它们验证实现和隔离协议，不代表真实模型跨任务收益或完整生产隔离认证。
