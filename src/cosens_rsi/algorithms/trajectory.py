"""Observable action traces, conservative diagnoses and declarative repairs.

Projected UI logs are intentionally not accepted as complete action telemetry.
Diagnoses are hypotheses; only paired execution can establish a useful repair.
"""
from __future__ import annotations

from .hierarchical import fingerprint


def measure_trace(trace: dict) -> dict:
    """Count semantic actions, never tokens, dollars or narrated reasoning.

    The trusted runner owns completeness and state hashes. Checks count toward
    path length but are excluded from redundant-action hypotheses.
    """
    if (not isinstance(trace, dict) or trace.get("schema") != "action-trace-v1"
            or trace.get("complete") is not True or not isinstance(trace.get("events"), list)):
        raise ValueError("complete action-trace-v1 telemetry required")
    events = trace["events"]
    if not events:
        raise ValueError("empty telemetry cannot measure an executed task")
    ids = set()
    seen = set()
    repeats = failures = repairs = 0
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or not isinstance(event.get("id"), str) or not event["id"]
                or event["id"] in ids or type(event.get("sequence")) is not int or event["sequence"] != index + 1
                or event.get("kind") not in {"action", "check"}
                or event.get("status") not in {"passed", "failed"}
                or any(not isinstance(event.get(k), str) or not event[k]
                       for k in ("node", "action", "input_hash", "state_before", "state_after"))
                or type(event.get("repair", False)) is not bool
                or event.get("error_kind") not in (None, "method", "infrastructure")):
            raise ValueError("invalid or incomplete action event")
        ids.add(event["id"])
        key = (event["node"], event["action"], event["input_hash"], event["state_before"])
        repeated = (event["kind"] == "action" and key in seen
                    and event["state_before"] == event["state_after"])
        repeats += repeated
        if event["kind"] == "action":
            seen.add(key)
        failures += event["status"] == "failed" and event.get("error_kind") == "method"
        repairs += event.get("repair", False)
    return {"schema": "exploration-metrics-v1", "trace_hash": fingerprint(trace),
            "actions": len(events), "failed_attempts": failures,
            "repeated_actions": repeats, "repairs": repairs}


def diagnose(train: list[dict], bundle: dict) -> dict:
    """Build train-only evidence cards; partial traces never justify pruning."""
    cards = []
    nodes = {n["id"]: n for n in bundle["flow"]["nodes"]}
    unavailable = []
    records = [(record, index, rollout) for record in train
               for index, rollout in enumerate(record.get("rollouts", [record]))]
    for parent, rollout_index, record in records:
        if record.get("task_id") != parent["task_id"]:
            raise ValueError("rollout task mismatch")
        if record.get("judge_valid") is False:
            unavailable.append(record["task_id"])
            continue
        # Independent artifact failures support a quality hypothesis even
        # without complete action telemetry. They never justify flow pruning.
        failed = [c["key"] for c in record.get("quality_checks", [])
                  if c.get("passed") is False and isinstance(c.get("key"), str)]
        if failed and (len(nodes) == 1 or record.get("execution_status") == "completed"):
            # The final artifact producer is a repair target, not a proven
            # root cause. Earlier steps may still be responsible for the gap.
            node = nodes[bundle["flow"]["final"].split(".", 1)[0]]
            cards.append({"mechanism_id": fingerprint({"kind": "artifact_quality_gap", "binding": node["skill"]}),
                          "kind": "artifact_quality_gap", "task_id": record["task_id"],
                          "node": node["id"], "binding": node["skill"], "rollout": rollout_index,
                          "criterion_ids": failed, "evidence_kind": "artifact_feedback", "hypothesis_only": True,
                          "counterexample": "A failed artifact check does not establish its root cause."})
        trace = record.get("action_trace")
        try:
            metrics = measure_trace(trace)
        except ValueError:
            unavailable.append(record["task_id"])
            continue
        seen = set()
        for event in trace["events"]:
            if event["node"] not in nodes or event.get("error_kind") == "infrastructure":
                continue
            key = (event["node"], event["action"], event["input_hash"], event["state_before"])
            kind = None
            if event["kind"] == "action" and key in seen and event["state_before"] == event["state_after"]:
                kind = "repeated_no_progress"
            elif event["status"] == "failed" and event.get("error_kind") == "method":
                kind = "method_failure"
            elif event.get("repair"):
                kind = "repair_required"
            if event["kind"] == "action":
                seen.add(key)
            if not kind:
                continue
            node = nodes[event["node"]]
            mechanism = fingerprint({"kind": kind, "binding": node["skill"]})
            cards.append({"mechanism_id": mechanism, "kind": kind,
                          "task_id": record["task_id"], "node": event["node"],
                          "rollout": rollout_index,
                          "binding": node["skill"], "event_ids": [event["id"]],
                          "trace_hash": metrics["trace_hash"], "hypothesis_only": True,
                          "counterexample": "The action may be required under a different observable state."})
        # Identical executed methods/inputs/outputs suggest a removable graph
        # splice. A valid DAG and independent replay still decide correctness.
        prior_nodes = {}
        for attempt in record.get("trace", []):
            if not attempt.get("passed") or attempt.get("node") not in nodes:
                continue
            key = tuple(attempt.get(k) for k in ("step_ref", "input_sha256", "output_sha256"))
            if not all(key):
                continue
            earlier = prior_nodes.get(key)
            if earlier and earlier != attempt["node"]:
                node = nodes[attempt["node"]]
                step = bundle["steps"][bundle["bindings"][node["skill"]]]
                refs = [e["id"] for e in trace["events"] if e["node"] == node["id"]]
                if refs:
                    cards.append({"mechanism_id": fingerprint({"kind": "duplicate_node", "node": node["id"]}),
                                  "kind": "duplicate_node", "task_id": record["task_id"], "node": node["id"],
                                  "binding": node["skill"], "replacement_node": earlier,
                                  "output_ports": list(step["outputs"]), "rollout": rollout_index,
                                  "event_ids": refs, "trace_hash": metrics["trace_hash"], "hypothesis_only": True,
                                  "counterexample": "Equal observed outputs may hide a necessary side effect."})
            prior_nodes.setdefault(key, attempt["node"])
    return {"schema": "trajectory-diagnosis-v1", "cards": cards,
            "incomplete_tasks": unavailable, "train_only": True}


def propose_repairs(payload: dict) -> dict:
    """A usable local seed proposer; model-based proposers use the same protocol.

    It changes procedural rules, never copies case-specific commands or answers.
    """
    context = payload.get("diagnostic_context", {})
    cards = context.get("cards", [])
    allowed = payload.get("allowed_layer")
    if allowed == "flow":
        cards = [c for c in cards if c["kind"] == "duplicate_node"]
    if not cards:
        return {"diagnosis": {"layer": "none", "reason": "no supported procedural repair"}, "changes": []}
    card = max(cards, key=lambda c: sum(x["mechanism_id"] == c["mechanism_id"] for x in cards))
    binding = card["binding"]
    body = payload["bundle"]["steps"][payload["bundle"]["bindings"][binding]]["body"]
    if card["kind"] in {"repeated_no_progress", "duplicate_node"}:
        alternatives = [
            ("steps", "Reuse an existing result when its inputs and observable state are unchanged; repeat an action only with a stated new information need."),
            ("recovery", "After an action produces no new evidence or state change, inspect its cause and choose a different method instead of repeating it unchanged."),
            ("preconditions", "Before exploration, inspect available intermediate results and identify the specific missing evidence needed for this node.")]
    else:
        alternatives = [
            ("preconditions", "Inspect the input format and tool prerequisites before choosing an execution method; select a method compatible with the observed inputs."),
            ("recovery", "Classify a failed attempt using observable errors, address its cause, and switch method when the same preconditions still fail."),
            ("checks", "Check the saved intermediate result against the node contract before sending it downstream; repair only a demonstrated gap.")]
    changes = []
    archive = {(a.get("base_sha256"), a.get("change_sha256")) for a in payload.get("archive", [])}
    if card["kind"] == "duplicate_node" and allowed != "step":
        removal = {"layer": "flow", "op": "remove", "node": card["node"], "value": {
            "redirects": {port: f"{card['replacement_node']}.{port}" for port in card["output_ports"]}}}
        if (fingerprint(payload["bundle"]), fingerprint(removal)) not in archive:
            changes.append(removal)
    for slot, rule in alternatives:
        if allowed == "flow":
            break
        if rule in body[slot]:
            continue
        change = {"layer": "step", "target": binding, "slot": slot, "value": [*body[slot], rule]}
        if (fingerprint(payload["bundle"]), fingerprint(change)) not in archive:
            changes.append(change)
    if len(changes) < 2:
        return {"diagnosis": {"layer": "none", "reason": "supported repairs already explored"}, "changes": []}
    layers = {c["layer"] for c in changes[:payload["max_candidates"]]}
    return {"diagnosis": {"layer": next(iter(layers)) if len(layers) == 1 else "mixed", "mechanism_id": card["mechanism_id"],
                          "reason": card["kind"], "expected_effect": "avoid demonstrated detours while preserving acceptance",
                          "counterexample": card["counterexample"]},
            "changes": changes[:payload["max_candidates"]]}
