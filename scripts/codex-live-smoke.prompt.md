执行一次本机 CoSens RSI 的集成测试。这是用户明确授权的测试任务。

业务操作只使用 cosens_rsi MCP 服务提供的 cosens_* 工具。不要使用旧的 rsi 服务或远程桌面，不修改工作区文件，不读取凭证，不调用管理员接口。测试中允许创建 RSI Run、checkpoint 和测试经验。如需加载插件的 skill，允许通过本机只读文件工具或 cat 读取当前仓库的 plugins/cosens-rsi/skills/rsi-run/SKILL.md；这是唯一允许的本机文件读取，不需要额外调研安装配置。

先调用 cosens_get_active_recipe，task_family 为 codex-deployment-check。找不到 Recipe 也算连接检查成功。

调用 cosens_prepare_task，objective 为“Local Codex RSI deployment smoke: read server state and record the observed action”，task_family 为 codex-deployment-check，execution_mode 为 assist。

调用 cosens_request_guidance，observation 为“Verify that Codex can inspect its current RSI Run”，提供两个候选：
- {"id":"read-run","version":"1","description":"Read the current RSI Run","confidence":0.9,"cost":0}
- {"id":"read-recipe","version":"1","description":"Read the active Recipe for this task family","confidence":0.5,"cost":0}

执行推荐的动作：read-run 调 cosens_get_run；read-recipe 调 cosens_get_active_recipe。然后调用 cosens_checkpoint，checkpoint 包含 decision_id、实际执行的 actual_action_id，以及 next_state={"server_read_succeeded":true}。

调用 cosens_get_decision 检查 actual 和 experience.revision。后台写回可能稍后完成，必要时最多再读取五次。experience.revision 至少为 1 才算经验写回通过。不提交任何自证 verified 奖励，不把本次测试视为用户验收。

调用 cosens_submit_result，记录这次连接/动作/经验写回测试结果。最后仅返回 JSON，字段为 status、run_id、decision_id、actual_action_id、experience_id、experience_revision、note。全部实际调用成功且 revision>=1 时 status=passed，否则 status=failed，并在 note 写明真实失败原因。不要伪造工具调用结果。
