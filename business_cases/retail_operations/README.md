# 电商经营月报：真实业务任务与 RSI 经验对照

任务是为电商运营负责人交付销售、负向记账、客户国家贡献和当月重复下单分析，以及有数据依据的行动建议。输入是 UCI Online Retail 的真实历史交易，并非用户当前企业数据。署名、许可与原文件哈希见 `data/raw/provenance.json`。

`protocol.json` 在执行前冻结了口径、候选、相同先验、训练月份、留出月份与奖励公式。RSI 的候选路线是每次扫描原始 Excel，或查询已有且经过哈希验证的 SQLite 索引。两者必须满足相同的精确金额与计数要求。

- 1、2 月用于平衡探索和外部评估；11 月用于经验前后的同任务对照。
- 基准与后验使用相同候选内容、版本、顺序、confidence、cost、数据与代码。初始同分按原始 Excel 在前的确定性规则处理，已预先披露。
- 校验器直接读取工作簿 OOXML，独立于引擎所用 openpyxl。完整索引的 13 个月均与其逐项一致。
- 每条路线的每次月报测量包含三个独立子进程的 wall time，含进程启动、解析/查询与指标计算。开始计时前的文件哈希核对、一次性下载、索引构建和模型/MCP 往返不在该计时中。一次性构建成本单独记录。
- 管理员评估器只验证指标精确一致与这段执行耗时，不验证商业建议质量，不声称用户已经验收。Codex 无管理员凭证，不自行写入 verified reward。
- 本案例激活的是本地通用 prepare/produce/verify seed，不是经业务质量验证或自动演化发布的 Recipe。

这是固定工具与固定先验条件下的经验路由对照，不是自由策略 Codex 与 RSI 的随机对照。不清空操作系统文件缓存，不推断统计显著性，不以局部计算加速代替完整任务交付时间或通用业务效果。

复算单个任务：

```bash
business_cases/retail_operations/.venv/bin/python business_cases/retail_operations/measure.py \
  --engine indexed_sqlite --period 2011-11 --repeats 3 --out output/reproduction.json
```

测量文件拒绝覆写。独立评估的管理操作脚本是 `evaluate_training.py`，只验证明确列出的两个训练任务；基准和留出结果不会在后验决策前写成训练奖励。
