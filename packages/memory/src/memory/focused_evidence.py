"""Bounded provenance routing over authorized, literal event records.

Dates select an origin candidate, not a supersession relation. The caller must
check ACL, revisions and excluded assertion spans before and after selection.
No benchmark categories, gold evidence, or generated facts are accepted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import math
import re

from .event_evidence import _sentences, _support_window
from .retrieval import bm25, lexemes


def _terms(text):
    result = set()
    for word in lexemes(text):
        # Retrieval normalization only; never rewrite the returned evidence.
        if len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 4 and word.endswith("ed"):
            word = word[:-2]
        elif len(word) > 4 and word.endswith("s"):
            word = word[:-1]
        if len(word) > 1:
            result.add(word)
    return result


def select_origin(query, sources, groups, dates, dense_scores, *, as_of=None):
    """Choose an early, strongly matching episode and explicitly linked changes.

    Different events sharing an actor or product do not become an evidence
    chain merely because both were recalled. Near-match thresholds are global
    module constants, independent of dataset and question IDs.
    """
    by_group = defaultdict(list)
    for item in sources:
        by_group[groups[item.refs[0].source_id]].append(item)
    if not by_group:
        return (), {"reason": "no_eligible_event_sources"}
    lexical = bm25(query, [item.text for item in sources])
    term_set = _terms(query)
    group_words = {g: set().union(*(_terms(item.text) for item in items)) for g, items in by_group.items()}
    weights = {term: 1 + math.log((len(by_group) + 1) /
               (1 + sum(term in words for words in group_words.values()))) for term in term_set}
    denominator = sum(weights.values()) or 1
    named = [' '.join(m.group().casefold().split()) for m in re.finditer(
        r"\b[A-Z][A-Za-z]+(?:[ \u202f]+[A-Z][A-Za-z]+)+\b", query)]
    metrics = {}
    for group, items in by_group.items():
        ids = {item.refs[0].source_id for item in items}
        words = group_words[group]
        body = '\n'.join(re.sub(r'(?mi)^(?:Date|Author|From|To|Cc):[^\n]*$', '', item.text) for item in items)
        normalized_body = ' '.join(body.casefold().split())
        day_values = [dates[sid] for sid in ids if dates.get(sid)]
        semantic = sorted((dense_scores[sid] for sid in ids if sid in dense_scores), reverse=True)[:3]
        lexical_values = sorted((score for item, score in zip(sources, lexical)
                                 if item.refs[0].source_id in ids), reverse=True)[:3]
        metrics[group] = {
            "semantic": sum(semantic) / len(semantic) if semantic else 0,
            "lexical": sum(lexical_values) / len(lexical_values),
            "coverage": sum(weights[t] for t in term_set & words) / denominator,
            "named_entities_match": all(name in normalized_body for name in named),
            "date": min(day_values) if day_values else None,
        }
    eligible = [g for g, m in metrics.items() if m['named_entities_match']
                and (not as_of or not m["date"] or m["date"] <= as_of)]
    if not eligible:
        return (), {"reason": "no_event_at_public_cutoff", "groups": metrics}
    use_dense = max(metrics[g]["semantic"] for g in eligible) > 0
    channel = "semantic" if use_dense else "lexical"
    best = max(metrics[g][channel] for g in eligible)
    semantic_near = [g for g in eligible if metrics[g][channel] >= .80 * best]
    # Fit the lexical gate within the semantic neighborhood. A keyword-rich
    # unrelated event must not set the cutoff for a semantic paraphrase.
    best_lexical = max(metrics[g]['lexical'] for g in semantic_near)
    for group in semantic_near:
        metrics[group]['match_score'] = (
            .80 * metrics[group][channel] / (best or 1)
            + .20 * metrics[group]['lexical'] / (best_lexical or 1))
    best_match = max(metrics[g]['match_score'] for g in semantic_near)
    near = [g for g in semantic_near if metrics[g]['coverage'] >= .20
            and metrics[g]['match_score'] >= .90 * best_match]
    if not near:
        # An uncertain match preserves the caller's existing recall instead
        # of manufacturing an origin from weak overlap.
        return (), {"reason": "insufficient_topic_coverage", "groups": metrics}
    root = min(near, key=lambda g: (metrics[g]["date"] or "9999", -metrics[g][channel],
                                    -metrics[g]["lexical"], str(g)))
    root_day = metrics[root]["date"]
    linked = []
    for group in eligible if not as_of else metrics:
        if (group == root or not metrics[group]['named_entities_match']
                or metrics[group]["coverage"] < max(.20, .80 * metrics[root]["coverage"])):
            continue
        day = metrics[group]["date"]
        if not root_day or not day or day <= root_day:
            continue
        text = "\n".join(item.text for item in by_group[group])
        explicit_link = root in text or re.search(
            r"\b(?:earlier|previous|prior|original)\s+(?:\w+\s+){0,3}"
            r"(?:claim|belief|assumption|statement|report|decision)\b.{0,180}"
            r"\b(?:incorrect|wrong|retract\w*|correct\w*|replac\w*|supersed\w*)\b", text, re.I | re.S)
        if explicit_link and metrics[group][channel] >= .80 * best:
            linked.append(group)
    linked.sort(key=lambda g: (metrics[g]["date"], str(g)))
    selected = [root, *linked[:2]]
    result = []
    for group in selected:
        ordered = sorted(by_group[group], key=lambda item: (
            -dense_scores.get(item.refs[0].source_id, 0), item.refs[0].source_id))
        for item in ordered:
            result.append(replace(item, metadata={**item.metadata,
                "event_focus_policy": "origin-near-topic-v1", "event_focus_group": group,
                "event_focus_role": "origin_candidate" if group == root else "explicit_change_candidate",
                "event_focus_metrics": metrics[group]}))
    return tuple(result), {"policy": "origin-near-topic-v1", "root_group": root,
                           "selected_groups": selected, "near_match_groups": near, "groups": metrics,
                           "date_semantics": "navigation only; no replacement relation inferred"}


def complete_source_passage(item, query, *, max_chars=3200):
    """Keep short original records whole; otherwise select a bounded full turn."""
    if len(item.text) <= max_chars:
        return replace(item, metadata={**item.metadata, "support_window_policy": "whole-source-v1"})
    spans = list(_sentences(item.text))
    scores = bm25(query, [item.text[a:b] for a, b in spans])
    anchor = max(range(len(spans)), key=lambda i: (scores[i], -i))
    return _support_window(item, query, spans, anchor, max_chars)
