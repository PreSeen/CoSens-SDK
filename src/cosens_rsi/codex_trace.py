"""Import observable Codex JSONL items, with credential removal and compact I/O."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .observability import CHECK_NAMES, facts, parsed, route, short

PRIVATE_COMMAND = re.compile(r"local\.env|auth\.json|config\.toml|\bprintenv\b|COSENS_\w*(?:TOKEN|KEY)|(?:^|\s)env(?:\s|$)", re.I)
SECRET = re.compile(r"(Bearer\s+)[^\s\"']+|((?:api[_-]?key|admin[_-]?token|attempt[_-]?token|access[_-]?token|password|secret)\s*[=:]\s*)[^\s,\"']+", re.I)


def scrub(text):
    return SECRET.sub(lambda m: (m.group(1) or m.group(2) or "") + "[隐藏]", str(text or ""))


def readable_output(value):
    text = scrub(value)
    obj = parsed(text)
    if isinstance(obj, dict):
        lines = [f"{x['label']}：{x['value']}" for x in facts(obj) + facts(obj.get('kpis'))]
        for key, label in CHECK_NAMES.items():
            if key in obj and isinstance(obj[key], bool):
                lines.append(label + ("：通过" if obj[key] else "：未通过"))
        for key in ("status", "check_error_type", "message", "error", "saved", "run_status"):
            if isinstance(obj.get(key), (str, int, float)):
                lines.append(f"{key}：{short(obj[key], 250)}")
        return "\n".join(lines[:24]) or f"返回 {len(obj)} 项记录，内部结构字段已省略。"
    if isinstance(obj, list):
        return f"返回 {len(obj)} 条记录，内部结构字段已省略。"
    if text.lstrip().startswith(("{", "[")) or len(re.findall(r'"[^"\n]+"\s*:', text)) > 3:
        # JSON mixed with log output is still not useful prose, and may contain
        # nested credentials. Do not expose it as an unparsed text fallback.
        return "已返回结构化执行记录；指标与文件结果在对应步骤展示。"
    return short(text, 5000) or "命令未输出正文。"


def tool_result(item):
    result = item.get("result") or {}
    if isinstance(result.get("structured_content"), dict):
        return result["structured_content"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            obj = parsed(block.get("text"))
            if isinstance(obj, dict):
                return obj
    return {}


def input_summary(value):
    lines = []
    if not isinstance(value, dict):
        return "未保存步骤输入正文"
    for name, content in value.items():
        if name in {"attempt_token", "authorization", "token"}:
            continue
        if isinstance(content, str):
            lines.append(f"{name}：{short(content, 2000)}")
        elif isinstance(content, list):
            names = [str(c.get("name") or "已保存工件") for c in content if isinstance(c, dict)]
            lines.append(f"{name}：" + "、".join(names))
    return "\n".join(lines) or "输入采用上一步保存的结果。"


def describe_mcp(name, arguments, result):
    if name == "cosens_prepare_task":
        context = result.get("context", {})
        return arguments.get("objective", "准备任务"), f"任务状态：{result.get('status', result.get('decision', '已返回'))}；知识检索 {len(context.get('wiki', [])) + len(context.get('evidence', []))} 条；Recipe：{result.get('recipe', {}).get('status', '未记录')}。"
    if name == "cosens_request_guidance":
        candidates = arguments.get("candidates") or []
        entry = arguments.get("observation", "") + "\n候选路线：\n" + "\n".join(f"- {c.get('description') or c.get('id')}" for c in candidates)
        decision = result.get("decision", {})
        lines = [f"推荐：{route(decision.get('recommended_action_id'))}", "尚无已验证经验" if decision.get("cold_start", True) else "参考已验证的历史任务经验"]
        for score in decision.get("scores", []):
            lines.append(f"{route(score.get('action_id'))}：初始 {score.get('base_score')} → 调整后 {score.get('score')}，引用 {len(score.get('experience_refs', []))} 条经验")
        return entry, "\n".join(lines)
    if name == "cosens_next_step":
        node = result.get("node", {})
        method = node.get("method", {})
        return "读取冻结流程中的下一步", (node.get("goal", result.get("status", "已返回")) + "\n" + "\n".join(str(s) for s in method.get("steps", [])))
    if name == "cosens_submit_step":
        return input_summary(arguments.get("outputs", {})), "步骤结果：" + result.get("status", "已返回") + ("；" + "；".join(result["errors"]) if result.get("errors") else "")
    if name == "cosens_store_artifact":
        return f"保存 {arguments.get('name', '交付物')}（{len(arguments.get('content', ''))} 字符；正文可在步骤产物中查看）", f"已保存：{result.get('name', '交付物')}"
    if name == "cosens_get_decision":
        writes = result.get("writes", [])
        receipt = parsed(writes[-1].get("receipt_json"), {}) if writes else {}
        return "读取真实动作及经验写回状态", f"写回：{'、'.join(w.get('state', '') for w in writes) or '未记录'}；奖励：{receipt.get('reward_status', '未记录')}"
    if name == "cosens_get_run_events":
        return "读取已持久化的过程记录", f"已返回 {len(result.get('items', []))} 条过程事件"
    if name == "cosens_get_run":
        return "读取任务与冻结上下文", f"任务状态：{result.get('status', '已返回')}"
    if name == "cosens_get_active_recipe":
        return "查找任务类型对应的执行流程", f"匹配：{result.get('status', '未记录')}；修订：{result.get('revision', '—')}"
    if name in {"cosens_complete_run", "cosens_submit_result"}:
        return "完成执行流程并提交交付结果", f"任务状态：{result.get('status', '已返回')}；业务验收未自动确认"
    return "调用 RSI 任务接口", f"接口已返回：{result.get('status', '完成')}"


def import_trace(service, path, principal="local-codex", *, run_id=None, node_id="prepare", user_prompt=None):
    from .capture import import_session_trace
    captured = import_session_trace(service, path, principal, run_id=run_id)
    if run_id is not None:
        service._authorize_evidence_write(run_id, principal)
    source = Path(path)
    events = []
    thread = source.name
    for line in source.read_text(errors="replace").splitlines():
        obj = parsed(line)
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "thread.started":
            thread = obj.get("thread_id", thread)
        if obj.get("type") == "item.completed":
            events.append(obj["item"])
    current, node, first = run_id, node_id, run_id
    staged, records = [], []
    for sequence, item in enumerate(events, 1):
        kind = item.get("type")
        if kind not in {"mcp_tool_call", "command_execution", "file_change", "agent_message"}:
            continue
        result = tool_result(item) if kind == "mcp_tool_call" else {}
        name = item.get("tool", "")
        if name == "cosens_prepare_task" and result.get("run_id"):
            current, node = result["run_id"], "prepare"
            first = first or current
        if name == "cosens_next_step" and result.get("node", {}).get("id"):
            node = result["node"]["id"]
        if name in {"cosens_complete_run", "cosens_submit_result"}:
            node = "writeback"
        status = "failed" if item.get("error") or item.get("status") == "failed" or (kind == "command_execution" and item.get("exit_code") not in (0, None)) or (item.get("result") or {}).get("isError") else "completed"
        if kind == "mcp_tool_call":
            # No unrelated connector traffic enters a business trace.
            if item.get("server") != "cosens_rsi":
                continue
            input_text, output_text = describe_mcp(name, item.get("arguments") or {}, result)
            label = "RSI · " + name.removeprefix("cosens_")
        elif kind == "command_execution":
            command = item.get("command", "")
            label = "运行命令"
            if PRIVATE_COMMAND.search(command):
                input_text, output_text = "本地认证或配置检查；凭证相关命令已隐藏。", "认证或配置检查已结束；相关输出不在业务追踪中公开。"
            else:
                input_text, output_text = short(scrub(command), 16000), readable_output(item.get("aggregated_output", ""))
        elif kind == "file_change":
            label = "保存文件"
            input_text = "\n".join(str(c.get("path", "文件")) for c in item.get("changes", []))
            output_text = "文件变更已保存。" if status == "completed" else "文件保存未完成。"
        else:
            label, input_text, output_text = "执行说明", "当前步骤的进度说明", short(scrub(item.get("text", "")), 5000)
        record = {"id": "trace_" + hashlib.sha256((thread + ':' + item.get("id", str(sequence))).encode()).hexdigest(),
                  "run_id": current, "sequence": sequence, "node_id": node, "tool": label, "status": status,
                  "input_text": scrub(input_text), "output_text": scrub(output_text), "source": source.name}
        (records if current else staged).append(record)
    if first:
        for record in staged:
            record["run_id"], record["node_id"] = first, "prepare"
            records.append(record)
    inserted = 0
    with service._db() as db:
        for record in records:
            if not db.execute("SELECT 1 FROM runs WHERE id=? AND principal=?", (record["run_id"], principal)).fetchone():
                continue
            changed = db.execute("""INSERT INTO execution_trace(id,run_id,sequence,node_id,tool,status,input_text,output_text,source)
                VALUES(:id,:run_id,:sequence,:node_id,:tool,:status,:input_text,:output_text,:source)
                ON CONFLICT(id) DO UPDATE SET node_id=excluded.node_id,sequence=excluded.sequence,source=excluded.source,input_text=excluded.input_text,
                    output_text=excluded.output_text,status=excluded.status""", record)
            inserted += changed.rowcount
    if first and user_prompt is not None:
        from .capture import _bounded_payload
        eid = "native-prompt:" + hashlib.sha256((thread + ":" + user_prompt).encode()).hexdigest()
        payload = _bounded_payload(service, principal, eid, {"text": scrub(user_prompt),
            "origin": "explicit_prompt_file", "redacted": scrub(user_prompt) != user_prompt})
        service.capture_session_events(first, principal, [{"id": eid, "kind": "user_prompt", "payload": payload}])
    return {"source": source.name, "items_imported": inserted, "runs": sorted({r["run_id"] for r in records if r['run_id']}),
            "session_capture": captured}
