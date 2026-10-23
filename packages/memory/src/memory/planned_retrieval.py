"""Public-query focus retrieval with final live revalidation and bounded packing.

No LLM or embedding service is called here. Supplements use an independently supplied topic vector and lexical discovery;
an original query vector is never reused for a different query. No inferred
relations, answers, or source validity dates are generated.
"""

from __future__ import annotations

from dataclasses import replace

from .models import EvidenceBundle
from .query_plan import plan_query
from .retrieval import RetrievalConfig


def _identity(item):
    return item.kind, item.text, item.revision, item.refs


def merge_pools(query, pools):
    """Source-level RRF prevents many chunks from masquerading as many sources.

    Each query family contributes once per source. Round-robin span admission
    gives distinct sources a chance before one long document consumes the cap.
    """
    by_source, scores, seen = {}, {}, set()
    for pool in pools:
        ranked_sources = set()
        for item in pool.items:
            if item.kind != "source" or len(item.refs) != 1 or item.revision != item.refs[0].revision:
                raise ValueError("planned retrieval requires single-source literal evidence")
            ref = item.refs[0]
            version = ref.revision, ref.content_hash
            # Keep versions separate until the authoritative ledger checks
            # them. Never let an old revision's rank boost its replacement.
            key = ref.source_id, *version
            if key not in ranked_sources:
                scores[key] = scores.get(key, 0.0) + 1 / (60 + len(ranked_sources) + 1)
                ranked_sources.add(key)
            identity = _identity(item)
            if identity not in seen:
                seen.add(identity)
                by_source.setdefault(key, []).append(item)
    ordered = sorted(by_source, key=lambda key: -scores[key])  # stable input-order ties
    items = []
    for rank in range(max((len(v) for v in by_source.values()), default=0)):
        for key in ordered:
            if rank < len(by_source[key]):
                items.append(replace(by_source[key][rank], score=scores[key]))
    return EvidenceBundle(query, tuple(items), 0, {"query_plan_policy": "public-focus-source-rrf-v1"})


def recall_planned(memory, actor, project, query, *, token_budget=2000, char_budget=6000,
                   rank_config=RetrievalConfig(), focus_vector=None, **options):
    """Recall an optional focused query and revalidate every span before use.

    Every intermediate recall has the SAME output budget as ordinary recall.
    Final combination runs under one fresh project lock; it does not reuse an
    earlier permission decision or stale source contents. The returned bundle
    is a read snapshot, just like ordinary recall, not a reusable ACL grant.
    """
    from .service import evidence_cost

    if type(token_budget) is not int or not 1 <= token_budget <= 32000:
        raise ValueError("token budget must be 1..32000")
    if type(char_budget) is not int or not 1 <= char_budget <= 128000:
        raise ValueError("char budget must be 1..128000")
    plan = plan_query(query)
    options.setdefault("use_graph", False)
    options.setdefault("use_wiki", False)
    if rank_config.source_context or ((options["use_graph"] or options["use_wiki"]) and not rank_config.materialize_derived):
        raise ValueError("planned recall requires literal materialization without context anchors")
    base = memory.recall(actor, project, query, token_budget=token_budget, char_budget=char_budget,
                         rank_config=rank_config, **options)
    pools = [base]
    for supplement in plan.supplementary_queries:
        lexical = {k: v for k, v in options.items() if k not in {
            "vector_index", "query_vector", "parent_vector_index", "parent_vector_limit",
            "parent_vector_chunk_limit", "candidate_rescorer", "reranker"}}
        if focus_vector is not None:
            lexical.update({k: v for k, v in options.items() if k in {
                'vector_index', 'parent_vector_index', 'parent_vector_limit', 'parent_vector_chunk_limit'}})
            lexical['query_vector'] = focus_vector
        pools.append(memory.recall(actor, project, supplement, token_budget=token_budget, char_budget=char_budget,
                                   rank_config=replace(rank_config, semantic_windows=False,
                                                       rerank_literal_sources=False, source_reserve=0), **lexical))
    pool = merge_pools(query, pools)
    if plan.focus and (plan.asks_for_evidence or plan.compare_changes):
        pool = memory.focus_event_evidence(
            actor, project, plan.focus, pool, parent_vector_index=options.get('parent_vector_index'),
            focus_vector=focus_vector, public_as_of=plan.as_of,
            valid_at=options.get('valid_at'), known_at=options.get('known_at'))
    with memory.db.transaction():
        memory._scope(actor, project)
        pool = memory._eligible_literal_pool(actor, project, pool,
                                              valid_at=options.get("valid_at"), known_at=options.get("known_at"))
        chosen, chars, cost = [], 0, 0
        for item in pool.items:
            item_cost, item_chars = evidence_cost((item,), preceding=bool(chosen))
            if chars + item_chars <= char_budget and cost + item_cost <= token_budget:
                chosen.append(item)
                chars += item_chars
                cost += item_cost
        return EvidenceBundle(query, tuple(chosen), cost, {
            **pool.diagnostics, "query_plan": plan.payload(), "recall_passes": len(pools),
            "context_chars": chars, "char_budget": char_budget, "token_budget": token_budget,
            "supplementary_retrieval": "focus-vector-and-lexical" if focus_vector is not None else "lexical-only",
            "memory_evidence_policy": "event_focus", "planning_model_calls": 0,
            "base_retrieval": base.diagnostics,
        })
