"""Synthetic, two-task reconciliation case; evaluator data never enters model prompts."""
import json


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_case(tag):
    company = f"星桥仿真企业-{tag}"
    prefix = f"[合成测试 {tag}] 企业={company}；任务簇=渠道结算对账。"
    sources = [
        ("current-policy", prefix + "来源：2026-09-01业务负责人确认的结算协议v2。仅适用于本企业2026-09-01起的渠道批次。青岚渠道自批次结算日后第2个自然日到期，赤砂渠道第4个自然日到期。截止日等于到期日时已到期。未到期且未到账列not_due，已到期零到账列missing，少到账列short_paid，全额到账列matched。自然日无需节假日日历。其他渠道无已知协议时列unknown_policy，不能推断天数。"),
        ("accounting", prefix + "来源：2026-09-03财务口径确认。所有金额用人民币整数分。退款状态R7表示已成功，P2表示处理中；只有R7计入实际退款。CSV的refund_signed_cents为带符号退款负数；JSON的refund_debit_cents为退款正数。应结算=订单总额-成功退款绝对值-渠道费用-本批次暂扣准备金。准备金不能当成少付；处理中退款不能扣减。不能直接以订单总额对比银行到账。"),
        ("counterparties", prefix + "来源：2026-09-04及09-20付款主体核验记录。银行付款方沪澜结算服务对应青岚渠道，栖原清分对应赤砂渠道，星渡渠道资金中心也对应青岚渠道。上述映射仅适用于本企业；其他名称即使金额和批次引用一致也不能自动认定主体，列unknown_party并列出unmatched_receipts。无映射不等于已证明款项不属于该批次。"),
        ("history-correction", prefix + "来源：2026-09-05对账用户纠错并经业务负责人确认。银行同一receipt_id重复导出只计算一次；不同receipt_id即使同批次、同金额也属于不同到账。批次可以分多笔到账，应先按receipt_id去重，再检查主体与渠道映射，最后按批次累计。未知主体的回执不计入已确认到账，保留待核，不得按金额强行配平。"),
        ("expired-policy", prefix + "来源：已废止的2026-08结算协议v1。仅适用于2026-08-31及之前批次，青岚为第5个自然日、赤砂第1个自然日。自2026-09-01起失效，不适用于9月或10月。"),
        ("other-company", f"[合成测试 {tag}] 企业=其他仿真企业；任务簇=渠道结算对账。来源：其他企业专属协议。青岚第1个自然日到期，沪澜结算服务属于赤砂。此记录与{company}无关，不可跨企业套用。"),
    ]
    a = {
        "company": company, "as_of": "2026-09-30", "currency": "CNY",
        "batches.csv": "batch_id,channel,settlement_date,gross_cents,refund_signed_cents,refund_state,fee_cents,held_reserve_cents\n"
        "S-101,青岚,2026-09-24,100000,-10000,R7,2000,3000\n"
        "S-102,赤砂,2026-09-28,70000,-5000,P2,1000,2000\n"
        "S-103,青岚,2026-09-25,50000,-1000,R7,1000,0\n"
        "S-104,青岚,2026-09-26,20000,0,R7,500,0\n",
        "bank.csv": "receipt_id,batch_ref,payer,amount_cents\n"
        "R-01,S-101,沪澜结算服务,40000\nR-02,S-101,沪澜结算服务,45000\n"
        "R-02,S-101,沪澜结算服务,45000\nR-03,S-103,沪澜结算服务,47000\n"
        "R-04,S-104,沪澜结算服务,19500\n",
    }
    b = {
        "company": company, "as_of": "2026-10-31", "currency": "CNY",
        "schema_notes": "本次JSON的refund_debit_cents是退款正数；transaction_ref是银行回执唯一号，batch_link引用批次。与9月CSV结构不同。",
        "settlements": [
            {"id": "X-201", "provider": "青岚", "closed_on": "2026-10-23", "sales_cents": 210000, "refund_debit_cents": 20000, "refund_state": "R7", "commission_cents": 3000, "reserve_cents": 5000},
            {"id": "X-202", "provider": "青岚", "closed_on": "2026-10-24", "sales_cents": 90000, "refund_debit_cents": 10000, "refund_state": "P2", "commission_cents": 2000, "reserve_cents": 1000},
            {"id": "X-203", "provider": "赤砂", "closed_on": "2026-10-29", "sales_cents": 60000, "refund_debit_cents": 5000, "refund_state": "R7", "commission_cents": 1000, "reserve_cents": 1000},
            {"id": "X-204", "provider": "青岚", "closed_on": "2026-10-26", "sales_cents": 80000, "refund_debit_cents": 5000, "refund_state": "R7", "commission_cents": 1000, "reserve_cents": 0},
            {"id": "X-205", "provider": "青岚", "closed_on": "2026-10-25", "sales_cents": 40000, "refund_debit_cents": 0, "refund_state": "R7", "commission_cents": 1000, "reserve_cents": 0},
            {"id": "X-206", "provider": "玄塔", "closed_on": "2026-10-25", "sales_cents": 10000, "refund_debit_cents": 0, "refund_state": "R7", "commission_cents": 200, "reserve_cents": 0},
        ],
        "bank_export": [
            {"transaction_ref": "N-01", "batch_link": "X-201", "counterparty": "星渡渠道资金中心", "cents": 91000},
            {"transaction_ref": "N-02", "batch_link": "X-201", "counterparty": "星渡渠道资金中心", "cents": 91000},
            {"transaction_ref": "N-02", "batch_link": "X-201", "counterparty": "星渡渠道资金中心", "cents": 91000},
            {"transaction_ref": "N-03", "batch_link": "X-204", "counterparty": "沪澜结算服务", "cents": 70000},
            {"transaction_ref": "N-04", "batch_link": "X-205", "counterparty": "陌生结算中心", "cents": 39000},
        ],
    }
    # Independent, literal reference ledger: not imported by the actor or exposed as tools.
    expected = {
        "task1": {
            "S-101": (85000, 85000, "2026-09-26", "matched"),
            "S-102": (67000, 0, "2026-10-02", "not_due"),
            "S-103": (48000, 47000, "2026-09-27", "short_paid"),
            "S-104": (19500, 19500, "2026-09-28", "matched"),
        },
        "task2": {
            "X-201": (182000, 182000, "2026-10-25", "matched"),
            "X-202": (87000, 0, "2026-10-26", "missing"),
            "X-203": (53000, 0, "2026-11-02", "not_due"),
            "X-204": (74000, 70000, "2026-10-28", "short_paid"),
            "X-205": (39000, 0, "2026-10-27", "unknown_party"),
            "X-206": (9800, 0, None, "unknown_policy"),
        },
    }
    contract = (
        "这是仿真企业任务。核对当期渠道结算与银行到账，历史业务事实只以Memory有来源记录为依据。"
        "检索时同时核对企业、适用日期、渠道和废止状态；缺少证据应明确unknown，不得猜测。"
        "输出严格JSON对象：rows数组每项包含batch_id,expected_cents,received_cents,due_date,status,evidence_ids；"
        "整数金额，未知金额或日期使用null。status允许matched,not_due,missing,short_paid,unknown_party,unknown_policy,unknown。"
        "evidence_ids引用实际Memory返回的id；同时输出unmatched_receipts数组（回执号）、duplicate_receipts数组（回执号）、"
        "summary_cents对象（expected_cents,received_cents，均为逐批已知金额求和），以及中文notes。"
        "不得将缺规则视为事实不存在，不得把候选经验当作适用于当前任务的已证实结论。"
    )
    return {"tag": tag, "company": company, "cluster": "channel-settlement-reconciliation", "sources": sources,
            "tasks": {"task1": a, "task2": b}, "contract": contract, "expected": expected,
            "expected_duplicates": {"task1": ["R-02"], "task2": ["N-02"]},
            "expected_unmatched": {"task1": [], "task2": ["N-04"]}}


def grade(case, task, output, allowed_evidence):
    """Check exact facts, evidence binding and conservation separately."""
    checks = []
    def check(key, passed):
        checks.append({"key": key, "passed": bool(passed)})
    rows = output.get("rows", []) if isinstance(output, dict) else []
    valid_rows = isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
    rows = rows if valid_rows else []
    by_id = {r.get("batch_id"): r for r in rows}
    reference = case["expected"][task]
    check("complete_unique_batches", valid_rows and len(by_id) == len(rows) == len(reference) and set(by_id) == set(reference))
    for bid, values in reference.items():
        row = by_id.get(bid, {})
        for name, value in zip(("expected_cents", "received_cents", "due_date", "status"), values):
            actual = row.get(name)
            check(f"{bid}:{name}", actual == value and (value is None or not isinstance(actual, bool)))
        refs = row.get("evidence_ids", [])
        check(f"{bid}:evidence", isinstance(refs, list) and bool(refs) and all(isinstance(x,str) and x in allowed_evidence for x in refs))
    check("duplicate_receipts", sorted(output.get("duplicate_receipts", [])) == case["expected_duplicates"][task])
    check("unmatched_receipts", sorted(output.get("unmatched_receipts", [])) == case["expected_unmatched"][task])
    summary = output.get("summary_cents", {})
    check("expected_conservation", summary.get("expected_cents") == sum(v[0] for v in reference.values()))
    check("received_conservation", summary.get("received_cents") == sum(v[1] for v in reference.values()))
    passed = sum(x["passed"] for x in checks)
    fact_checks = [x for x in checks if not x["key"].endswith(":evidence")]
    evidence_checks = [x for x in checks if x["key"].endswith(":evidence")]
    return {"passed": passed, "total": len(checks), "strict_pass": passed == len(checks), "checks": checks,
            "fact_passed": sum(x["passed"] for x in fact_checks), "fact_total": len(fact_checks),
            "evidence_passed": sum(x["passed"] for x in evidence_checks), "evidence_total": len(evidence_checks),
            "unknown_rows": sum(r.get("status") == "unknown" for r in rows),
            "failed_keys": [x["key"] for x in checks if not x["passed"]]}


def feedback_for(result):
    """Describe observed failure classes; never manufacture a failed initial run."""
    failed = result["failed_keys"]
    if not failed:
        return "模拟业务复核：本轮独立业务检查全部通过；这是复核确认，不虚构失败原因。请保留实际检索、口径确认和检查方法。"
    return ("模拟业务用户反馈（不是实际用户验收）：独立检查发现" + ",".join(failed) +
            "。请核查是否检索并确认了生效版本、主体映射、成功退款状态、准备金口径及回执去重规则。"
            "对原因的描述是待验证假设；依据实际Memory来源定位错误，修订方法后重新核对。")
