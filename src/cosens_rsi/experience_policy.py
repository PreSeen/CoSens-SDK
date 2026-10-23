"""Conservative candidate reranking; confidence is a score, not a logit."""
import hashlib
import json
import math

from memory.experiences import action_signature, normalize_actions


DEFAULT_POLICY = {"schema": "experience-rerank-v2", "mode": "advisory", "beta": 0.5,
                  "support_prior": 3.0, "cost_weight": 0.1, "unseen_reward_prior": 0.0}


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(dump(value).encode()).hexdigest()


def rank_candidates(candidates, experiences, policy):
    candidates = normalize_actions(candidates)
    groups = {action_signature(a): {} for a in candidates}
    for item in experiences:
        record = item["record"]
        outcome = record.get("outcome", {})
        if outcome.get("status") != "verified" or record.get("action_signature") not in groups:
            continue
        value = outcome.get("value")
        weight = item.get("similarity", 0)
        if (type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1
                or type(weight) not in (int, float) or not math.isfinite(weight) or weight <= 0):
            continue
        # Repeated attempts on the same task cannot inflate independent support.
        lineage = record.get("task_lineage_id", record["run_id"])
        group = groups[record["action_signature"]]
        if lineage not in group or weight > group[lineage][0]:
            group[lineage] = (weight, float(value), item)
    samples = [sample for group in groups.values() for sample in group.values()]
    values = {}
    for key, group in groups.items():
        total = sum(w for w, _, _ in group.values())
        values[key] = sum(w * v for w, v, _ in group.values()) / total if total else None
    # An untried route has a neutral prior, not the outcome of the only route
    # tried so far. Otherwise one verified failure cancels itself in the
    # baseline and can never change a subsequent decision.
    if policy.get("schema") == "experience-rerank-v2":
        baseline = sum(q if q is not None else policy.get("unseen_reward_prior", 0.0)
                       for q in values.values()) / len(values)
    else:
        baseline = sum(w * v for w, v, _ in samples) / sum(w for w, _, _ in samples) if samples else 0
    scale = max((abs(q - baseline) for q in values.values() if q is not None), default=1) or 1
    scores = []
    for action in candidates:
        key = action_signature(action)
        group = groups[key]
        weights = [w for w, _, _ in group.values()]
        effective = sum(weights) ** 2 / sum(w * w for w in weights) if weights else 0
        support = effective / (effective + policy["support_prior"])
        advantage = (values[key] - baseline) / scale if values[key] is not None else 0
        score = action["confidence"] + policy["beta"] * support * advantage - policy["cost_weight"] * action["cost"]
        scores.append({"action_id": action["id"], "base_score": action["confidence"], "score": score,
                       "q": values[key], "advantage": advantage, "independent_tasks": len(group),
                       "support": support, "experience_refs": [
                           {k: item[k] for k in ("experience_id", "revision", "content_hash")}
                           for _, _, item in group.values()]})
    selected = max(scores, key=lambda item: item["score"])
    recommended = selected["action_id"]
    if policy.get("mode") == "shadow":
        recommended = max(candidates, key=lambda a: a["confidence"] - policy["cost_weight"] * a["cost"])["id"]
    return {"recommended_action_id": recommended, "experience_recommended_action_id": selected["action_id"],
            "selection_mode": policy.get("mode", "advisory"), "scores": scores,
            "policy_version": digest(policy), "mechanism": "candidate_score_reranking",
            "recommendation_only": True, "cold_start": not samples}
