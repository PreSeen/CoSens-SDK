"""Read-only case/trace projections. Never interpret task completion as acceptance."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re

from .quality_observability import quality_comparison, quality_evidence
from .performance_observability import verified_supplier_measurement


ACTION_NAMES = {"raw_xlsx": "逐次扫描原始 Excel", "indexed_sqlite": "查询已校验的 SQLite 索引",
                "supplier_csv_scan": "逐供应商扫描 CSV", "supplier_sqlite_batch": "新建索引后批量核对",
                "patch_then_regress": "故障定位后最小修复与回归", "invariants_then_replay": "业务不变量与回放测试驱动修复"}
NODE_NAMES = {"prepare": "准备执行计划", "produce": "执行与产出", "verify": "核验交付物"}
CHECK_NAMES = {
    "frozen_input_hashes_unchanged": "输入资料保持不变", "same_november_measurement_hashes_and_kpis": "前后使用相同数据与统计口径",
    "baseline_and_post_three_trials_exact": "前后三轮测量均精确一致", "monthly_csv_exact": "月度指标逐项一致",
    "html_embedded_data_all_months_exact": "看板内嵌数据与指标一致",
    "offline_no_external_resources": "看板可离线使用", "builder_reproduces_exact_bytes": "复算产物一致",
    "artifact_local_hashes_match": "本地文件与 RSI 工件一致", "all_monthly_csv_exact": "全部月份指标一致",
    "country_csv_exact": "国家指标一致", "html_scripts_parse": "看板脚本语法通过",
    "frozen_inputs_unchanged": "输入保持不变", "quality_equal": "前后业务指标相同",
    "exact_oracle_match": "独立指标核对通过", "monthly_csv_rows": "月度记录数", "country_csv_rows": "国家记录数",
}
EVENT_NAMES = {"prepared": "任务已触发，冻结上下文", "step_issued": "发出执行步骤", "decision_recommended": "给出策略建议",
    "actual_action_recorded": "记录实际执行路线", "step_passed": "步骤输出通过合同校验", "step_rejected": "步骤输出需修复",
    "step_failed": "步骤失败", "checkpoint": "保存进度", "submitted": "提交任务结果", "guided_completed": "任务完成",
    "decision_evaluated": "独立评估已记录", "feedback": "记录业务反馈"}


def short(value, length=180):
    text = str(value or "").strip()
    return text if len(text) <= length else text[:length].rstrip() + "…"


def parsed(value, default=None):
    try:
        return json.loads(value) if value else default
    except (ValueError, TypeError):
        return default


def refs(value):
    if isinstance(value, dict):
        if isinstance(value.get("artifact_id"), str):
            yield value["artifact_id"]
        for child in value.values():
            yield from refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from refs(child)


def route(action):
    return ACTION_NAMES.get(action, str(action or "尚未记录"))


def facts(value):
    """A small business-oriented field list, never an unfiltered object dump."""
    if not isinstance(value, dict):
        return []
    result = []
    fields = {"period": "统计月份", "engine": "计算路线", "median_seconds": "三次测量中位耗时", "raw_rows": "原始交易行",
              "eligible_rows": "纳入统计行", "excluded_rows": "排除行", "positive_order_count": "正向订单数",
              "known_customer_count": "已识别客户", "repeat_customer_count": "当月重复下单客户",
              "passed": "验收通过组数", "failed": "验收未通过组数", "total": "验收总组数",
              "supplier_count": "待核供应商", "invoice_count": "待核发票", "exception_count": "异常记录",
              "duplicate_delivery_count": "已合并的重复投递", "index_build_median_seconds": "索引构建耗时（已计入总计）",
              "sla_seconds": "批次计算目标（秒）"}
    for key, label in fields.items():
        if key in value and isinstance(value[key], (str, int, float)):
            number = value[key]
            display = route(number) if key == "engine" else f"{number:.4f} 秒" if key == "median_seconds" and isinstance(number, (int, float)) else str(number)
            result.append({"label": label, "value": display})
    for key, label in {"gross_positive_micros": "正向记账额", "credit_abs_micros": "负向记账额", "net_micros": "净记账额"}.items():
        if isinstance(value.get(key), (int, float)):
            result.append({"label": label, "value": f"£{value[key] / 1000000:,.2f}"})
    for key, label in {"overpaid_cents": "超付待核金额", "underpaid_cents": "少付及未付金额",
                       "missing_invoice_cents": "无票付款", "supplier_mismatch_cents": "供应商错配付款"}.items():
        if type(value.get(key)) is int:
            result.append({"label": label, "value": f"¥{value[key] / 100:,.2f}"})
    if value.get("known_customer_count"):
        result.append({"label": "已识别客户当月重复下单率", "value": f"{value.get('repeat_customer_count', 0) / value['known_customer_count']:.2%}"})
    return result


class CaseObservability:
    def init_observability_schema(self):
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS execution_trace(
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,node_id TEXT,tool TEXT NOT NULL,status TEXT NOT NULL,
                input_text TEXT NOT NULL,output_text TEXT NOT NULL,source TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS execution_trace_run ON execution_trace(run_id,sequence)")

    def _case_key(self, row):
        task = parsed(row["task_json"], {})
        environment = task.get("environment", {})
        group = environment.get("case_id") or environment.get("case")
        identity = [row["principal"], row["project"], row["family"], str(group)] if group else [row["id"]]
        return "case_" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]

    def _case_rows(self, principal, case_id=None):
        with self._db() as db:
            rows = db.execute("SELECT * FROM runs WHERE principal=? ORDER BY created_at", (principal,)).fetchall()
        return [r for r in rows if case_id is None or self._case_key(r) == case_id]

    def list_cases(self, principal, limit=100):
        groups = {}
        with self._db() as db:
            evolving = {r[0] for r in db.execute("SELECT resource_id FROM jobs WHERE kind='learning' AND state IN ('queued','running')")}
        for row in self._case_rows(principal):
            groups.setdefault(self._case_key(row), []).append(row)
        result = []
        for cid, rows in groups.items():
            last = rows[-1]
            task = parsed(last["task_json"], {})
            title = task.get("environment", {}).get("case_title") or re.split(r"[，；;。：:]", task.get("objective", "任务"))[0]
            completed = sum(r["status"] == "completed" for r in rows)
            result.append({"case_id": cid, "title": short(title, 60), "run_count": len(rows), "completed_count": completed,
                "status": "ready" if any(r['id'] in evolving for r in rows) else "completed" if completed == len(rows) else "failed" if any(r["status"] == "failed" for r in rows) else "cancelled" if any(r["status"] == "cancelled" for r in rows) else "ready",
                "updated_at": max(r["updated_at"] for r in rows)})
        return sorted(result, key=lambda x: x["updated_at"], reverse=True)[:max(1, min(int(limit), 200))]

    def _run_material(self, row):
        run = dict(row)
        for source, target, default in [("task_json", "task", {}), ("snapshot_json", "snapshot", {}),
                                        ("checkpoint_json", "checkpoint", {}), ("result_json", "result", {})]:
            run[target] = parsed(row[source], default)
        with self._db() as db:
            bundle = db.execute("SELECT bundle_json FROM recipes WHERE id=?", (row["recipe_id"],)).fetchone()
            run["bundle"] = parsed(bundle[0], {}) if bundle else {}
            run["events"] = [{"sequence": r["seq"], "type": r["kind"], "data": parsed(r["body_json"], {}), "time": r["created_at"]}
                for r in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (row["id"],))]
            run["decisions"] = []
            artifact_ids = set(refs(run["checkpoint"])) | set(refs(run["result"]))
            artifact_ids.update(i for i in run["task"].get("input_artifact_ids", []) if isinstance(i, str))
            for d in db.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY rowid", (row["id"],)):
                item = dict(d)
                item["guidance"] = parsed(d["response_json"], {})
                item["actual"] = parsed(d["actual_json"], {})
                item["writes"] = [dict(w) for w in db.execute("SELECT kind,state,receipt_json,last_error FROM memory_outbox WHERE decision_id=? ORDER BY created_at,rowid", (d["id"],))]
                item["evaluations"] = [parsed(e[0], {}) for e in db.execute("SELECT body_json FROM decision_evaluations WHERE decision_id=?", (d["id"],))]
                for evaluation in item["evaluations"]:
                    if evaluation.get("evidence_artifact_id"):
                        artifact_ids.add(evaluation["evidence_artifact_id"])
                run["decisions"].append(item)
            run["attempts"] = [dict(r) for r in db.execute("SELECT node_id,attempt_no,status,error_json,created_at FROM step_attempts WHERE run_id=? ORDER BY rowid", (row["id"],))]
            run["tools"] = [dict(r) for r in db.execute("SELECT sequence,node_id,tool,status,input_text,output_text FROM execution_trace WHERE run_id=? ORDER BY sequence,id", (row["id"],))]
            run["jobs"] = [dict(r) for r in db.execute("SELECT kind,state FROM jobs WHERE resource_id=?", (row["id"],))]
            run["feedback"] = [parsed(r[0], {}) for r in db.execute("SELECT body_json FROM feedback WHERE run_id=? ORDER BY created_at", (row["id"],))]
            run["artifacts"] = {}
            evaluation = db.execute('SELECT e.*,a.content FROM run_recipe_evaluations e JOIN artifacts a ON a.id=e.evidence_artifact_id WHERE e.run_id=? AND a.principal=?', (row['id'],row['principal'])).fetchone()
            run['recipe_evaluation'] = parsed(evaluation['content'],{}) if evaluation else None
            run['recipe_memory_writes'] = [dict(r) for r in db.execute('SELECT state,receipt_json,partition_name FROM recipe_memory_outbox WHERE run_id=? AND job_id IS NULL ORDER BY created_at', (row['id'],))]
            if evaluation:artifact_ids.add(evaluation['evidence_artifact_id'])
            if artifact_ids:
                marks = ",".join("?" for _ in artifact_ids)
                for a in db.execute(f"SELECT id,name,content,content_hash FROM artifacts WHERE principal=? AND id IN ({marks})", (row["principal"], *artifact_ids)):
                    run["artifacts"][a["id"]] = dict(a)
        measurements = []
        for artifact in run["artifacts"].values():
            value = parsed(artifact["content"])
            if isinstance(value, dict) and all(k in value for k in ("engine", "period", "median_seconds", "trials", "kpis")):
                measurements.append(value)
        run["measurement"] = next((m for m in measurements if any(d["actual"].get("actual_action_id") == m["engine"] for d in run["decisions"])), None)
        return run

    def _artifact_card(self, artifact):
        content, name = artifact["content"], artifact["name"]
        value = parsed(content)
        kind = "html" if name.lower().endswith(".html") else "markdown" if name.lower().endswith(".md") else "csv" if name.lower().endswith(".csv") else "text"
        summary = ""
        highlights = []
        if isinstance(value, (dict, list)):
            kind = "record"
            if isinstance(value, list):
                return {"artifact_id": artifact["id"], "name": name, "kind": kind,
                        "summary": f"记录列表（{len(value)} 项），未展开内部结构", "highlights": [], "auxiliary": True}
            highlights = facts(value) + facts(value.get("kpis"))
            if isinstance(value.get("trials"), list):
                trials = value["trials"]
                if trials:
                    highlights.append({"label": "独立指标核对", "value": f"{sum(t.get('exact_oracle_match') is True for t in trials if isinstance(t, dict))}/{len(trials)} 次通过"})
            for key, label in CHECK_NAMES.items():
                if key in value and isinstance(value[key], (bool, int)):
                    highlights.append({"label": label, "value": "通过" if value[key] is True else "未通过" if value[key] is False else str(value[key])})
            summary = "独立业务验收结果" if value.get('schema') == 'code-repair-quality-v1' else "指标与测量记录" if value.get("kpis") else "核验记录" if highlights else "技术记录；省略哈希、版本及内部字段"
        elif kind == "markdown":
            lines = [line.strip() for line in content.splitlines() if line.strip() and not line.startswith(("#", "|", "-", "```"))]
            summary = short(lines[0] if lines else "中文说明文档", 180)
        elif kind == "csv":
            summary = "可查看的指标明细表"
        elif kind == "html":
            summary = "交互式业务看板"
        else:
            summary = "执行脚本" if name.endswith((".py", ".js")) else short(content, 100)
        return {"artifact_id": artifact["id"], "name": name, "kind": kind, "summary": summary,
                "highlights": highlights[:16], "auxiliary": name == '执行记录.md' or kind in {"text", "record"} and not highlights}

    def _io(self, value, artifacts, label="输入"):
        if isinstance(value, str):
            return [{"label": label, "text": value}]
        if isinstance(value, list):
            cards = [self._artifact_card(artifacts[aid]) for aid in dict.fromkeys(refs(value)) if aid in artifacts]
            return [{"label": label, "artifacts": cards}] if cards else [{"label": label, "text": "无已保存的文件引用"}]
        if isinstance(value, dict):
            result = []
            for key, child in value.items():
                if key in {"attempt_token", "token", "authorization"}:
                    continue
                name = {"task": "任务目标", "brief": "执行简报", "files": "交付物", "final": "最终交付", "summary": "结果说明"}.get(key, key)
                if isinstance(child, (str, list)):
                    result.extend(self._io(child, artifacts, name))
            return result or [{"label": label, "text": "此步骤未保存可展示的正文"}]
        return [{"label": label, "text": "尚未记录"}]

    def _decision_view(self, decision, experience_sources):
        guidance, actual = decision["guidance"], decision["actual"]
        recommendation = guidance.get("decision", {})
        candidates = {c["id"]: c for c in guidance.get("candidates", [])}
        routes = []
        for score in recommendation.get("scores", []):
            candidate = candidates.get(score["action_id"], {})
            sources = []
            for ref in score.get("experience_refs", []):
                source = experience_sources.get(ref["experience_id"])
                sources.append({"run_id": source["run_id"] if source else None,
                    "title": source["title"] if source else "已验证的历史任务经验", "revision": ref.get("revision")})
            routes.append({"name": route(score["action_id"]), "action_id": score["action_id"], "description": candidate.get("description", ""),
                "base_score": score.get("base_score"), "score": score.get("score"), "independent_tasks": score.get("independent_tasks", 0),
                "sources": sources})
        receipts = [parsed(w["receipt_json"], {}) for w in decision["writes"]]
        reward = receipts[-1].get("reward_status", "pending") if receipts else "not_recorded"
        states = [w["state"] for w in decision["writes"]]
        return {"decision_id": decision["id"], "observation": guidance.get("state", {}).get("observation", ""),
            "recommended": route(recommendation.get("recommended_action_id")), "actual": route(actual.get("actual_action_id")),
            "basis": recommendation.get("explanation", {}).get("basis"),
            "deviated": bool(actual and actual.get("actual_action_id") != recommendation.get("recommended_action_id")),
            "cold_start": recommendation.get("cold_start", True), "routes": routes, "reward_status": reward,
            "write_status": "not_recorded" if not states else "failed" if "failed" in states else "done" if all(s == "done" for s in states) else "pending",
            "experience_revision": decision["experience_revision"]}

    def _run_view(self, run, sources):
        outputs = run["checkpoint"].get("guided_outputs", {})
        stages = []
        trigger = next((e for e in run["events"] if e["type"] == "prepared"), None)
        snapshot = run["snapshot"]
        all_views = [self._decision_view(d, sources) for d in run["decisions"]]
        stages.append({"id": "trigger", "title": "任务触发", "status": "passed" if trigger else "unknown",
            "summary": "用户显式发起 RSI 任务" if trigger and trigger["data"].get("explicit") else "根据已记录的触发结果开始执行",
            "inputs": [{"label": "业务目标", "text": run["task"].get("objective", "")}],
            "outputs": [{"label": "执行方式", "text": "分层引导执行" if run["execution_mode"] == "guided" else "辅助执行"},
                        {"label": "冻结的执行流程", "text": f"Recipe 修订 {run['recipe_revision']}" if run["recipe_revision"] else "未匹配到 Recipe，按辅助模式执行"}], "tools": []})
        knowledge = []
        for item in snapshot.get("wiki", []) + snapshot.get("evidence", []):
            knowledge.append({"title": short(item.get("title") or item.get("source_kind") or "知识候选", 60),
                              "text": short(item.get("summary") or item.get("text") or item.get("content") or "已保存来源引用", 350)})
        matched = sum(len(r["sources"]) for d in all_views for r in d["routes"])
        procedures=snapshot.get('recipe_memory',{}).get('items',[])
        stages.append({"id": "memory", "title": "Memory 检索与冻结", "status": "passed", "summary": f"知识候选 {len(knowledge)} 条 · 决策引用已验证经验 {matched} 条",
            "inputs": [{"label": "检索问题", "text": snapshot.get("query", run["task"].get("objective", ""))}],
            "outputs": [{"label": "知识检索", "text": f"Wiki {len(snapshot.get('wiki', []))} 条，扩展证据 {len(snapshot.get('evidence', []))} 条。返回候选不代表执行时采用。"},
                        {"label": "经验检索", "text": "本轮未发起候选动作评分，按冻结的 Recipe 执行。" if not all_views else f"策略决策引用 {matched} 条已验证经验。" if matched else "本轮没有可用的已验证经验，按初始评分选择。"}],
            "knowledge": knowledge, "tools": []})
        if 'recipe_memory' in snapshot:
            stages[-1]['summary']+=f' · 已发布方法经验 {len(procedures)} 条'
            stages[-1]['outputs'].append({'label':'Recipe 方法经验','text':'\n'.join(f"Memory {p['experience_id']} · 修订 {p['revision']}\n采用 Recipe v{p['lesson']['revision']}：{p['lesson'].get('diagnosis','')}" for p in procedures) or '未检索到与当前 Recipe 匹配的已发布方法经验。'})
        nodes = run["bundle"].get("flow", {}).get("nodes", []) if run["execution_mode"] == "guided" else []
        for node in nodes:
            inputs = {}
            for port, binding in node.get("inputs", {}).items():
                if binding == "$task":
                    inputs[port] = run["task"].get("objective", "")
                elif isinstance(binding, str) and "." in binding:
                    parent, key = binding.split(".", 1)
                    inputs[port] = outputs.get(parent, {}).get(key, "前置步骤尚未产出")
            attempts = [a for a in run["attempts"] if a["node_id"] == node["id"]]
            state = attempts[-1]["status"] if attempts else "waiting"
            decisions = [self._decision_view(d, sources) for d in run["decisions"] if d["node_id"] == node["id"]]
            stages.append({"id": node["id"], "title": NODE_NAMES.get(node["id"], node.get("goal") or node["id"]), "status": state,
                "summary": "输出合同已通过" if state == "passed" else "等待执行" if state == "waiting" else "当前步骤执行中或需要修复",
                "inputs": self._io(inputs, run["artifacts"]), "outputs": self._io(outputs.get(node["id"], {}), run["artifacts"], "输出"),
                "decisions": decisions, "attempts": len(attempts), "errors": [error for a in attempts for error in parsed(a["error_json"], [])],
                "tools": [t for t in run["tools"] if t["node_id"] == node["id"]]})
        if not nodes:
            stages.append({"id": "assist", "title": "执行与阶段结果", "status": "passed" if run["status"] == "completed" else "pending",
                "summary": "按已记录的检查点展示实际执行", "inputs": [{"label": "任务", "text": run["task"].get("objective", "")}],
                "outputs": self._io(run["checkpoint"], run["artifacts"], "阶段结果"), "decisions": all_views, "tools": run["tools"]})
        quality = quality_evidence(run)
        if quality:
            assessment, _, evidence = quality
            stages.append({"id": "independent_acceptance", "title": "独立业务验收", "status": "passed" if assessment['failed'] == 0 else "failed",
                "summary": f"操作员在代码提交后独立回放：{assessment['passed']}/{assessment['total']} 组通过，与 Codex 自测分开。",
                "inputs": [{"label": "验收对象", "text": "本次提交的不可变代码工件；固定公开业务需求与独立回放集。"}],
                "outputs": [{"label": "业务边界核验", "text": '\n'.join(('✓ ' if c['passed'] else '✗ ')+c['title']+'：'+c.get('detail', '') for c in assessment['checks'])}],
                "tools": []})
        recipe_quality=run.get('recipe_evaluation')
        if recipe_quality:
            checks=recipe_quality.get('quality_checks',[])
            stages.append({'id':'recipe_quality','title':'独立业务质量验收','status':'passed' if recipe_quality.get('task_success') else 'failed',
                'summary':f"{sum(c.get('passed') is True for c in checks)}/{len(checks)} 项检查通过。可信评估通道绑定本次实际交付物和冻结 Recipe。",
                'inputs':[{'label':'验收对象','text':'本次保存的交付结果；执行结束后运行固定独立业务规则。'}],
                'outputs':[{'label':'逐项检查','text':'\n'.join(('通过' if c['passed'] else '未通过')+' · '+c['title'] for c in checks)}]})
        unbound = [t for t in run["tools"] if t["node_id"] not in {s["id"] for s in stages} | {"writeback"}]
        stages[0]["tools"] = unbound if nodes else []
        stages.append({"id": "writeback", "title": "经验写回与交付" if all_views else "交付与学习状态", "status": "passed" if run["status"] == "completed" else "pending",
            "summary": "实际动作已记录；经验写回与业务验收分开显示" if all_views else "保存实际交付；Recipe 改进与评测在独立实验中记录。", "inputs": [{"label": "实际执行", "text": "；".join(d["actual"] for d in all_views) or "按冻结方法执行，本轮没有进行候选路线重排。"}],
            "outputs": self._io(run["result"].get("final", run["result"]), run["artifacts"], "最终交付"),
            "writebacks": all_views, "tools": [t for t in run["tools"] if t["node_id"] == "writeback"],
            "acceptance": "已记录业务反馈（不作为独立奖励或验收证明）" if run["feedback"] else "尚无用户业务验收记录"})
        if run.get('recipe_memory_writes'):
            stages[-1]['title']='经验写回 Memory 与交付'
            stages[-1]['summary']='本次真实执行及独立评估写入版本化 Memory；用户业务验收另行记录。'
            stages[-1]['outputs'].append({'label':'Memory 写回','text':'\n'.join(
                f"{'已写回' if w['state']=='done' else '等待或需要重试'} · {parsed(w['receipt_json'],{}).get('experience_id','未完成')} · 修订 {parsed(w['receipt_json'],{}).get('revision','未完成')}"
                for w in run['recipe_memory_writes'])})
        measurement = run["measurement"]
        timeline = []
        for event in run["events"]:
            data = event["data"]
            detail = NODE_NAMES.get(data.get("node_id"), data.get("node_id", ""))
            if event["type"] in {"decision_recommended", "actual_action_recorded"}:
                detail = route(data.get("actual_action_id") or data.get("recommended_action_id"))
            elif event["type"] == "decision_evaluated":
                detail = "程序指标已独立验证" if data.get("status") == "verified" else "尚未验证"
            timeline.append({"sequence": event["sequence"], "time": event["time"], "title": EVENT_NAMES.get(event["type"], event["type"]), "detail": detail})
        return {"run_id": run["id"], "recipe_id": run["recipe_id"], "recipe_revision": run["recipe_revision"],
            "task_family": run["family"], "title": short(run["task"].get("objective", "任务"), 110), "objective": run["task"].get("objective", ""),
            "executor_model": run['task'].get('environment', {}).get('executor_model'),
            "status": run["status"], "created_at": run["created_at"], "stages": stages, "timeline": timeline,
            "measurement": {"period": measurement.get("period"), "seconds": measurement.get("median_seconds"), "facts": facts(measurement.get("kpis"))} if measurement else None,
            "quality": {"passed": sum(c.get('passed') is True for c in recipe_quality.get('quality_checks',[])), "total": len(recipe_quality.get('quality_checks',[]))} if recipe_quality else {"passed": quality[0]['passed'], "total": quality[0]['total']} if quality else None,
            "tool_count": len(run["tools"]), "trace_note": "已关联真实 Codex 工具日志" if run["tools"] else "当前只保存了 RSI 阶段记录，未采集 Codex 工具日志"}

    def _comparison(self, runs):
        quality = quality_comparison(runs, route)
        if quality:
            return quality
        candidates = []
        trusted = set()
        for run in runs:
            for decision in run["decisions"]:
                delivered = [parsed(w["receipt_json"], {}) for w in decision["writes"] if w["state"] == "done"]
                if delivered and delivered[-1].get("reward_status") == "verified":
                    trusted.add((decision["experience_id"], delivered[-1].get("revision")))
            m = run["measurement"]
            if not m:
                continue
            for d in run["decisions"]:
                g = d["guidance"]
                if d["actual"].get("actual_action_id") == m.get("engine") and g.get("decision", {}).get("recommended_action_id") == m.get("engine"):
                    candidates.append((run, d, m))
        for after in reversed(candidates):
            ar, ad, am = after
            if ad["guidance"]["decision"].get("cold_start", True):
                continue
            used = [ref for score in ad["guidance"]["decision"].get("scores", []) for ref in score.get("experience_refs", [])]
            if not used or any((ref.get("experience_id"), ref.get("revision")) not in trusted for ref in used):
                continue
            for before in candidates:
                br, bd, bm = before
                if br["created_at"] >= ar["created_at"] or not bd["guidance"]["decision"].get("cold_start", False):
                    continue
                required = ["period", "source_sha256", "cache_sha256", "engine_sha256", "measure_sha256", "oracle_sha256", "protocol_sha256"]
                same = all(bm.get(k) is not None and bm.get(k) == am.get(k) for k in required)
                same &= bd["guidance"].get("candidates") == ad["guidance"].get("candidates")
                same &= bd["guidance"].get("contract_hash") == ad["guidance"].get("contract_hash")
                same &= br["snapshot"].get("experience_context", {}).get("environment") == ar["snapshot"].get("experience_context", {}).get("environment")
                same &= br["recipe_id"] == ar["recipe_id"] and br["recipe_revision"] == ar["recipe_revision"]
                same &= bd["guidance"]["decision"].get("policy_version") == ad["guidance"]["decision"].get("policy_version")
                same &= bd["guidance"].get("state", {}).get("observation") == ad["guidance"].get("state", {}).get("observation")
                if not isinstance(bm.get("trials"), list) or not isinstance(am.get("trials"), list):
                    continue
                trials = bm["trials"] + am["trials"]
                correct = isinstance(bm.get("kpis"), dict) and bool(bm["kpis"]) and bm["kpis"] == am.get("kpis") and bool(trials) and all(isinstance(t, dict) and t.get("exact_oracle_match") is True for t in trials)
                seconds = [bm.get("median_seconds"), am.get("median_seconds")]
                if not same or not correct or not all(type(s) in (int, float) and math.isfinite(s) and s > 0 for s in seconds):
                    continue
                supplier = bm.get("schema") == am.get("schema") == "supplier-reconciliation-performance-v1"
                if supplier and not (verified_supplier_measurement(br, bd, bm) and verified_supplier_measurement(ar, ad, am)):
                    continue
                return {"available": True, "period": am["period"], "before_run_id": br["id"], "after_run_id": ar["id"],
                    "before_route": route(bm["engine"]), "after_route": route(am["engine"]), "before_seconds": seconds[0], "after_seconds": seconds[1],
                    "speedup": seconds[0] / seconds[1], "reduction": 1 - seconds[1] / seconds[0], "quality_equal": True,
                    "recommendation_changed": bm["engine"] != am["engine"], "trials": [len(bm["trials"]), len(am["trials"])],
                    "experience_count": sum(len(s.get("experience_refs", [])) for s in ad["guidance"]["decision"].get("scores", [])),
                    "metric_label": "批次核对计算速度比（含索引构建）" if supplier else "分析程序计算速度比",
                    "sla_seconds": 2.0 if supplier else None,
                    "scope": "仿真中文供应商对账；比较同一账套的两次执行，第一次实测反馈写回 Memory 后重放。计时包含 CSV 读取、每轮从零导入数据库与建立索引、核对及异常 CSV 输出；不含模型/MCP、文件哈希与独立参考核对。三轮新子进程中位数，非统计显著性结论。固定候选同分首次选第一项；只证明该账套上的选路和计算效率变化，不代表跨任务泛化、中文建议质量提升或 Recipe 演化。" if supplier else "页面按任务保存的测量工件核对相同输入与业务指标，非服务托管复评。这里比较分析程序耗时，不含模型/MCP、哈希核对、下载及索引构建；固定工具和先验的局部对照，不代表整体业务收益或通用能力提升。"}
        return {"available": False, "reason": "尚无满足相同输入、口径、候选和独立质量校验的经验前后对照。不会仅凭任务完成宣称 RSI 有提升。"}

    def get_case(self, case_id, principal):
        rows = self._case_rows(principal, case_id)
        if not rows:
            raise LookupError("case not found")
        runs = [self._run_material(r) for r in rows]
        sources = {}
        for run in runs:
            for decision in run["decisions"]:
                sources[decision["experience_id"]] = {"run_id": run["id"], "title": short(run["task"].get("objective"), 65)}
        comparison = self._comparison(runs)
        views = [self._run_view(r, sources) for r in runs]
        for index, (view, raw) in enumerate(zip(views, runs)):
            verified = any(w.get("reward_status") == "verified" for d in raw["decisions"] for w in [parsed(x["receipt_json"], {}) for x in d["writes"]])
            online = raw["task"].get("environment", {}).get("experiment_design") == "online-feedback-replay"
            view["role"] = "使用经验后" if view["run_id"] == comparison.get("after_run_id") else "经验前基准" if view["run_id"] == comparison.get("before_run_id") else ("首次执行" if index == 0 else "后续执行") if online else "经验校准" if verified else "任务执行"
        main = runs[-1]
        seen = set()
        deliverables = []
        for aid in refs(main["result"]):
            if aid in main["artifacts"] and aid not in seen:
                deliverables.append(self._artifact_card(main["artifacts"][aid]))
                seen.add(aid)
        title = next((line.lstrip('# ').strip() for a in main["artifacts"].values() if a["name"].endswith('.md')
                      and not any(w in a["name"].lower() for w in ['效果', 'verification', 'audit'])
                      for line in a["content"].splitlines() if line.startswith('# ')), None)
        summary = next((c for c in self.list_cases(principal, 200) if c["case_id"] == case_id), {})
        explicit_title = main['task'].get('environment', {}).get('case_title')
        evolution = self.recipe_evolution_view(case_id, principal)
        if evolution:
            for view in views:
                view['role'] = (f"Recipe v{view['recipe_revision']} · 原始交付" if view['recipe_id'] == evolution['before_recipe_id'] else
                                f"Recipe v{view['recipe_revision']} · 新方法交付")
        display_objective = main['task'].get('objective', '')
        if main['task'].get('environment', {}).get('experiment_design') == 'recipe-evolution-quality':
            display_objective = display_objective.split('\n', 1)[0]
        return {**summary, "title": explicit_title or title or summary.get("title", "业务任务"), "objective": short(display_objective, 350),
            "runs": views, "comparison": comparison, "deliverables": deliverables,
            "recipe_evolution": evolution,
            "lifecycle": self.recipe_lifecycle_view(case_id,principal,runs,views,evolution),
            "trace_coverage": {"run_count": len(runs), "tool_count": sum(len(r["tools"]) for r in runs),
                               "event_count": sum(len(r["events"]) for r in runs)}}

    def get_case_artifact(self, case_id, artifact_id, principal):
        if artifact_id.startswith('harness:'):
            return self.get_case_harness_artifact(case_id, artifact_id, principal)
        rows = self._case_rows(principal, case_id)
        artifact = None
        for row in rows:
            material = self._run_material(row)
            if artifact_id in material["artifacts"]:
                artifact = material["artifacts"][artifact_id]
                break
        if artifact is None:
            raise LookupError("case artifact not found")
        card = self._artifact_card(artifact)
        kind = card["kind"]
        if kind == "csv":
            reader = csv.DictReader(io.StringIO(artifact["content"].lstrip('\ufeff')))
            all_rows = list(reader)
            card.update(columns=(reader.fieldnames or [])[:25], rows=all_rows[:200], row_count=len(all_rows))
        elif kind in {"markdown", "html", "text"}:
            card["content"] = artifact["content"]
        else:
            value = parsed(artifact["content"], {})
            card["notes"] = [short(v, 350) for v in value.get("limits", []) if isinstance(v, str)][:8] if isinstance(value, dict) else []
        return card

    def get_case_run_recipe(self, case_id, run_id, principal):
        """Return the immutable Recipe revision frozen by a Run in this Case."""
        row = next((r for r in self._case_rows(principal, case_id) if r["id"] == run_id), None)
        if row is None:
            raise LookupError("case run not found")
        if not row["recipe_id"]:
            raise LookupError("run has no frozen recipe")
        with self._db() as db:
            recipe = db.execute("SELECT id,family,revision,bundle_hash,bundle_json,source FROM recipes WHERE id=?",
                                (row["recipe_id"],)).fetchone()
        if recipe is None:
            raise LookupError("frozen recipe not found")
        return {"recipe_id": recipe["id"], "task_family": recipe["family"],
                "revision": recipe["revision"], "bundle_hash": recipe["bundle_hash"],
                "source": recipe["source"], "bundle": parsed(recipe["bundle_json"], {})}
