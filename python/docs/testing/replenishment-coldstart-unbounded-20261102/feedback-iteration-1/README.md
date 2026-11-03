# 合成用户反馈：先改Recipe，再让Agent执行

原任务：Task1无Memory冷启动，输出312件并说明采购口径未经核验。

本轮通过已安装cosens-sdk 0.3.0调用线上服务：提交模拟业务反馈、模拟确认原因，基于V1生成同一recipe_id的V2，待V2完成后调用start_revision。实际交付的method哈希与V2一致，再执行模型修订并提交结果。没有向反馈直接写入标准答案240。

V2第一次生成因JSONDecodeError失败，显式恢复同一候选一次后成功；原失败记录保留。V2新增真实采购参数核验、实际可售时长核验、在途ETA与新增采购提前期区分、真实保护期计算等方法。修订结果为240件。

- feedback.txt：实际提交的模拟反馈。
- confirmed-cause.txt：模拟原因确认，不是真实用户验收。
- recipe-v1.json / recipe-v2.json：两版真实Recipe。
- answer.txt：Agent拿到V2后的实际回答。
- evaluation.json：版本关系、方法交付、答案与限制的核验。
- generation-attempt-1-failed.json：原生成失败记录。
- state.json：SDK调用回执。

本轮没有写入额外Memory或执行Task2，方法仍是未经独立验证的候选。反馈本身补充了业务参数，所以同任务订货量纠正不等于已证明Recipe的独立增益或跨任务迁移效果。
