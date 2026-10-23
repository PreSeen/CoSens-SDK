"""Experimental source-preserving shortlist, over already authorized evidence.

No database access, authorization, model calls or question-category dispatch.
This module does not replace recall or perform final context packing.
"""

from dataclasses import asdict, dataclass, replace
import hashlib
import json

from .models import Evidence
from .retrieval import RetrievalConfig, rank, shortlist_fingerprint


@dataclass(frozen=True)
class PoolShortlist:
    items: tuple[Evidence, ...]
    source_pool_sha256: str
    derived_pool_sha256: str
    shortlist_sha256: str


def candidate_pool_fingerprints(candidates, vector_scores):
    """Bind the multiset of authorized ranking inputs, independent of UUIDs."""
    pools = {'source': [], 'derived': []}
    for item in candidates:
        record = dict(kind=item.kind, text=item.text, revision=item.revision,
                      refs=[asdict(ref) for ref in item.refs],
                      search_text=item.metadata.get('search_text', item.text),
                      vector_score=vector_scores.get(item.id))
        pools['source' if item.kind == 'source' else 'derived'].append(
            json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return {name: dict(count=len(records), sha256=hashlib.sha256(
        json.dumps(sorted(records), ensure_ascii=False).encode()).hexdigest())
        for name, records in pools.items()}


def source_preserving_shortlist(
    query: str, candidates: list[Evidence], vector_scores: dict[str, float],
    config: RetrievalConfig, *, source_reserve: int,
) -> PoolShortlist:
    """Rank two independent pools; reserve raw slots within the same total cap.

    Each pool contributes at most rerank_limit candidates. Raw priority slots
    are followed by derived slots, then unused capacity is backfilled from raw
    and derived in that order. Duplicate text cannot consume two slots. Scores
    from independent corpora are never compared or averaged here. A caller must
    apply one common relevance scorer before final evidence packing.
    """
    if (type(source_reserve) is not int or not 1 <= source_reserve <= config.rerank_limit):
        raise ValueError('source reserve must be an integer within the total candidate cap')
    local = replace(config, redundancy_weight=0, source_weight=0, rerank_literal_sources=False, source_reserve=0)
    pools = [[item for item in candidates if item.kind == 'source'],
             [item for item in candidates if item.kind != 'source']]
    ranked = []
    for pool in pools:
        ids = {item.id for item in pool}
        ranked.append(rank(query, pool, {k: v for k, v in vector_scores.items() if k in ids}, local))
    raw, derived = ranked
    selected, seen = [], set()

    def take(items, maximum):
        added = 0
        for item in items:
            key = ' '.join(item.text.split()).casefold()
            if key in seen:
                continue
            if added == maximum or len(selected) == config.rerank_limit:
                break
            seen.add(key)
            metadata = {k: v for k, v in item.metadata.items() if k != 'shortlist_sha256'}
            metadata['candidate_pool'] = 'source' if item.kind == 'source' else 'derived'
            selected.append(replace(item, metadata=metadata))
            added += 1

    take(raw, source_reserve)
    take(derived, config.rerank_limit - source_reserve)
    take(raw, config.rerank_limit)
    take(derived, config.rerank_limit)
    return PoolShortlist(tuple(selected), shortlist_fingerprint(raw),
                         shortlist_fingerprint(derived), shortlist_fingerprint(selected))
