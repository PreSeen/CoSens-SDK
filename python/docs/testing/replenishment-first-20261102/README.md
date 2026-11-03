# 补货案例：真实首轮执行

通过已安装的 cosens-sdk 0.3.0、线上 US 服务和 deepseek-v4-flash 执行一次 Task1。没有提供反馈、Recipe 或标准答案，没有重试挑选失败结果。Memory 的5条合成资料通过SDK写入，Agent自主调用真实召回工具。

首轮给出240件，正确使用72小时可售时长、40件日需求、9天保护期、232件原始订货量，并识别在途到货前的缺货风险。预设的主要缺陷没有出现，因此本次不能作为已观察到的纠错增益样本。

- `answer.txt`：原始最终回答。
- `evaluation.json`：人工核验记录及独立标准值。
- `state.json`：SDK回执和实际工具记录，已剔除凭据。
- `model-request-*.json` / `model-response-*.json`：两次模型调用记录；同属一次Agent执行。
- `recipe-status.json`：任务提交后初始Recipe状态的真实回读。
- `run.py`：实验执行函数，连接参数通过函数参数传入；未附凭据文件。

没有执行反馈迭代或Task2。本记录不估计统计失败率或RSI质量提升。
