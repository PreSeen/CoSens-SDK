# CoSens 首页 SVG 动画脚本

状态：独立设计预览，未替换线上首页。

表达目标：用户在与 Agent 沟通和使用的过程中，自己的资料成为背景，反馈形成新的经验，经验改善下一次做事的方法。

| 时间 | 画面与台词 | 动态关系 |
| --- | --- | --- |
| 0–4.5 秒 | **You ask.** 用户问：“Can we launch the pilot on Friday?” | You → Your agent 连线亮起。 |
| 4.5–10 秒 | **Your experience comes into context.** 带回项目背景：试点保持小范围、onboarding 简单、离线导出未确认。 | Your experience → Memory → Your agent 依次成为当前高亮路径。 |
| 10–15 秒 | **The answer fits your situation.** AI：“Confirm offline export first. Keep the pilot small and onboarding simple. Then we can set a launch date.” | Your agent → You，高亮从获取背景转向交付帮助。 |
| 15–20 秒 | **You shape how it helps.** 用户：“Before suggesting dates, check unresolved dependencies.” | You → Your agent，共用连线上显示 Feedback，显示具体纠偏。 |
| 20–26 秒 | **Experience becomes a better method.** 提炼“定日期之前先检查依赖”的候选方法，展示 Develop → Validate → Reuse。 | Agent → Learn，同时 Memory → Learn 提供相关经验。说明验证之后才可复用，画面为流程演示。 |
| 26–31 秒 | **A new task. Experience carried forward.** 用户改问 dashboard 能否周一发布。 | You → Your agent，同时 Learn → Your agent。新任务使用自己的背景。 |
| 31–37.5 秒 | **A better next step.** AI：“Let’s check unresolved dependencies first. What is still waiting on approval or testing? Then we can choose a date.” | 改进的方法回到 Agent，再由 Agent 回答用户。旧项目的离线导出事实不迁移到新项目。 |

全程使用黑白界面与蓝色高亮。非当前路径保持低对比，避免所有箭头同时运动。Agent（Reason）、Memory 与 Learn 共同置于 Your second brain 虚线框内；You 位于左上，Your experience 位于左下。用户与 Agent 之间共用一条双向连接，Prompt / Reply / Feedback 随当前消息显示。所有流动信息都显示对应 label，Memory 与 Learn 之间有双向连接。

播放一轮后停止，可 Replay、Pause、Next，或点击七个场景位置。支持深浅两种配色；减少动态效果偏好下默认暂停并取消流动虚线。

实现文件：`index.html` 为 SVG 界面，`script.mjs` 为场景数据与播放控制。原型不调用真实模型、上传或记忆接口；Learn 为预览，Apps 为未来连接。先评审桌面动效，正式整合首页时再做手机布局。


2026-10-18 定位修订：本关系动画用于 Build → How it works，不再作为 Connect 普通用户首页演示；虚线框标题统一为 Your second brain。


最新命名修订：图内整体区域标题改为 CoSens SDK，替代 Your second brain；内容继续用于 Build → How it works。
