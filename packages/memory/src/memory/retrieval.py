"""Rank authorized evidence and pack literal passages behind one internal seam.

No source discovery, database access or permissions here: MemoryService must
perform current ACL, revision and temporal checks BEFORE invoking this module.
BM25 statistics therefore describe only the authorized candidate pool, not the
whole corpus. RRF is applied only when a real vector result is supplied.
"""

from __future__ import annotations

import math
import re
import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass, replace

from .models import Evidence
from .semantic_passages import select_passages


@dataclass(frozen=True)
class RetrievalConfig:
    """Global numeric module parameters, never inferred from question/category IDs."""

    # None preserves the complete indexed source span through scoring/packing.
    passage_chars: int | None = 560
    redundancy_weight: float = 0.15
    source_weight: float = 0.15
    rerank_limit: int = 200
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    dense_neighbor_radius: int = 0
    materialize_derived: bool = False
    source_context: bool = False
    rerank_literal_sources: bool = False
    source_reserve: int = 0
    semantic_windows: bool = False
    budget_feasible: bool = False

    def __post_init__(self):
        if type(self.budget_feasible) is not bool:
            raise ValueError('budget_feasible must be boolean')
        if self.budget_feasible and (self.semantic_windows or self.materialize_derived):
            raise ValueError('budget feasibility requires fixed literal raw representations')
        if self.passage_chars is None and (self.semantic_windows or self.materialize_derived):
            raise ValueError('whole source units require raw evidence without semantic windows or derived materialization')
        if type(self.semantic_windows) is not bool:
            raise ValueError('semantic_windows must be boolean')
        if self.semantic_windows and (self.source_reserve or self.materialize_derived or self.rerank_literal_sources):
            raise ValueError('semantic windows require raw-source selection without derived materialization')
        if type(self.source_reserve) is not int or not 0 <= self.source_reserve <= self.rerank_limit:
            raise ValueError('source_reserve must be an integer within the candidate cap')
        if self.source_reserve and not (self.materialize_derived and self.rerank_literal_sources):
            raise ValueError('source reservation requires literal scoring and materialization')
        if type(self.rerank_literal_sources) is not bool:
            raise ValueError("rerank_literal_sources must be boolean")
        if self.rerank_literal_sources and not self.materialize_derived:
            raise ValueError("literal reranking requires materialize_derived")
        if type(self.source_context) is not bool:
            raise ValueError("source_context must be boolean")
        if type(self.materialize_derived) is not bool:
            raise ValueError("materialize_derived must be boolean")
        if type(self.dense_neighbor_radius) is not int or not 0 <= self.dense_neighbor_radius <= 2:
            raise ValueError("dense_neighbor_radius must be an integer from 0 to 2")
        if (self.passage_chars is not None and (type(self.passage_chars) is not int or not 1 <= self.passage_chars <= 2_000_000)) or not 1 <= self.rerank_limit <= 200:
            raise ValueError("invalid passage/rerank budget")
        if not all(math.isfinite(w) and 0 <= w <= 1 for w in (self.redundancy_weight, self.source_weight)):
            raise ValueError("weights must be finite and within [0,1]")
        # Channel weights are relative, not probabilities.  Mem++ publishes
        # lexical/vector weights of 1/4, so values above one are valid.
        if not all(math.isfinite(w) and 0 <= w <= 100 for w in (self.lexical_weight, self.dense_weight)):
            raise ValueError("retrieval channel weights must be finite and within [0,100]")
        if self.lexical_weight + self.dense_weight == 0:
            raise ValueError("at least one retrieval channel required")


STOP = frozenset(
    """a an and are as at be been being by can could did do does for
from had has have how i if in into is it its may of on or our should than that the
their them then there these they this those to was were what when where which who
why will with would you your tell me about please""".split()
)


def lexemes(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9_]+", text.lower())
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return [word for word in words if word not in STOP]


def bm25(query: str, documents: list[str]) -> list[float]:
    """Standard BM25 (k1=1.2, b=.75), fitted to this authorized pool only."""
    counts = [Counter(lexemes(text)) for text in documents]
    q = set(lexemes(query))
    df = Counter(term for count in counts for term in count if term in q)
    n = len(counts)
    lengths = [sum(count.values()) for count in counts]
    average = sum(lengths) / max(1, n) or 1
    return [
        sum(
            math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            * count[t]
            * 2.2
            / (count[t] + 1.2 * (0.25 + 0.75 * length / average))
            for t in sorted(q)
            if count[t]
        )
        for count, length in zip(counts, lengths)
    ]


def passage(item: Evidence, query: str, max_chars: int | None = 560) -> Evidence:
    """Choose one contiguous literal source span; never rewrite a claim/wiki.

    A reference to the original chunk is narrowed by exactly the same offsets.
    No generated summary is presented as a quotation.
    """
    if max_chars is None or item.kind != "source" or len(item.text) <= max_chars or len(item.refs) != 1:
        return item
    text = item.text

    def word_char(char):
        return char.isascii() and char.isalnum()

    q = set(lexemes(query))
    # Start windows at sentence boundaries and just before query-bearing tokens.
    starts = {0, max(0, len(text) - max_chars)}
    boundaries = [m.end() for m in re.finditer(r"[.!?。！？]\s+|\n+", text)]
    starts.update(boundaries)
    hits = []
    for match in re.finditer(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text):
        word = match.group().lower()
        if word in q:
            hits.append((match.start(), match.end(), word))
        if "\u4e00" <= word[0] <= "\u9fff":
            hits.extend(
                (match.start() + i, match.start() + i + 2, word[i : i + 2])
                for i in range(len(word) - 1)
                if word[i : i + 2] in q
            )
    starts.update(max(0, begin - max_chars // 3) for begin, _, _ in hits)
    windows = []
    for start in sorted(starts):
        if start >= len(text):
            continue
        end = min(len(text), start + max_chars)
        if end < len(text):
            complete = [b for b in boundaries if start + max_chars // 2 <= b <= end]
            if complete:
                end = complete[-1]
        if start and word_char(text[start - 1]) and word_char(text[start]):
            while start < end and word_char(text[start]):
                start += 1
        if end < len(text) and word_char(text[end - 1]) and word_char(text[end]):
            while end > start and word_char(text[end - 1]):
                end -= 1
        if start < end:
            windows.append((start, end))
    # Unique query coverage, not repetition. Stable tie-breaking keeps order.
    start, end = max(
        windows or [(0, max_chars)],
        key=lambda span: (
            len({term for begin, finish, term in hits if span[0] <= begin and finish <= span[1]}),
            span[1] - span[0],
            -span[0],
        ),
    )
    ref = item.refs[0]
    return replace(
        item,
        text=text[start:end],
        refs=(replace(ref, start=ref.start + start, end=ref.start + end),),
        metadata={**item.metadata, "passage_offsets": [start, end]},
    )


def shortlist_fingerprint(items):
    """Ordered content/provenance identity, excluding transient storage IDs."""
    records = [dict(kind=item.kind, text=item.text, revision=item.revision,
                    refs=[asdict(ref) for ref in item.refs]) for item in items]
    return hashlib.sha256(json.dumps(records, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def rank(
    query: str,
    candidates: list[Evidence],
    vector_scores: dict[str, float],
    config: RetrievalConfig = RetrievalConfig(),
    reranker=None,
    rerank_text=None,
) -> list[Evidence]:
    """BM25 + optional rank fusion, followed by relevance/diversity selection."""
    if config.source_reserve:
        raise ValueError('source reservation must select independent pools before common reranking')
    if config.semantic_windows and reranker is None:
        raise ValueError('semantic windows require a scorer')
    if config.rerank_literal_sources and (reranker is None or not callable(rerank_text)):
        raise ValueError("literal reranking requires a reranker and an authorized evidence renderer")
    if not candidates:
        return []
    # Persisted assertion IDs may be random across equivalent reconstructions.
    # Resolve score ties by evidence content/provenance, not storage identity.
    stable_keys = [
        (item.kind, item.text,
         tuple((ref.source_id, ref.revision, ref.start, ref.end, ref.content_hash) for ref in item.refs),
         item.revision)
        for item in candidates
    ]
    lexical = bm25(query, [item.metadata.get("search_text", item.text) for item in candidates])
    lexical_order = sorted(range(len(candidates)), key=lambda i: (-lexical[i], stable_keys[i]))
    lex_ranks = {i: r for r, i in enumerate((i for i in lexical_order if lexical[i] > 0), 1)}
    dense_order = sorted(
        (i for i, item in enumerate(candidates) if item.id in vector_scores),
        key=lambda i: (-vector_scores[candidates[i].id], stable_keys[i]),
    )
    dense_ranks = {i: r for r, i in enumerate(dense_order, 1)}
    scores = [
        (config.lexical_weight / (60 + lex_ranks[i]) if i in lex_ranks else 0)
        + (config.dense_weight / (60 + dense_ranks[i]) if i in dense_ranks else 0)
        if dense_ranks
        else config.lexical_weight * lexical[i]
        for i in range(len(candidates))
    ]
    maximum = max(scores) or 1
    remaining = []
    parents = []
    seen = set()
    for i in sorted(range(len(candidates)), key=lambda i: (-scores[i], stable_keys[i])):
        if scores[i] <= 0:
            continue
        item = passage(candidates[i], query, config.passage_chars)
        key = " ".join(item.text.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        parents.append(candidates[i])
        remaining.append(
            (
                replace(
                    item,
                    score=scores[i],
                    metadata={**item.metadata, "retrieval": "bm25_rrf" if dense_ranks else "bm25", "bm25": lexical[i]},
                ),
                scores[i] / maximum,
                set(lexemes(item.text)),
                0.0,
            )
        )
        if len(remaining) == config.rerank_limit:
            break
    parent_hash = shortlist_fingerprint(parents)
    remaining = [(replace(item, metadata={**item.metadata, 'parent_shortlist_sha256': parent_hash}),
                  relevance, words, overlap) for item, relevance, words, overlap in remaining]
    if config.semantic_windows:
        selected = select_passages(query, parents, reranker, max_chars=config.passage_chars)
        remaining = [(replace(item, metadata={**item.metadata, 'parent_shortlist_sha256': parent_hash,
                                              'semantic_score': item.score}),
                      item.score, set(lexemes(item.text)), 0.0) for item in selected]
        # The window scorer already provided relevance; never score twice.
        return _finish_rank(query, remaining, config, None, None)
    return _finish_rank(query, remaining, config, reranker, rerank_text)


def rerank_shortlist(query, items, config, reranker, rerank_text):
    """Apply one common scorer to an already selected authorized shortlist."""
    if reranker is None or not callable(rerank_text):
        raise ValueError('preselected pool candidates require a common scorer and evidence renderer')
    if len(items) > config.rerank_limit:
        raise ValueError('preselected shortlist exceeds candidate cap')
    remaining = [(item, 0.0, set(lexemes(item.text)), 0.0) for item in items]
    return _finish_rank(query, remaining, config, reranker, rerank_text)


def _finish_rank(query, remaining, config, reranker, rerank_text):
    output, sources = [], Counter()
    # Bounded reranking: recall is context-limited; avoid quadratic work on 5k claims.
    remaining = remaining[: config.rerank_limit]
    fingerprint = shortlist_fingerprint([item for item, _, _, _ in remaining])
    remaining = [(replace(item, metadata={**item.metadata, 'shortlist_sha256': fingerprint}),
                  relevance, words, overlap) for item, relevance, words, overlap in remaining]
    if reranker is not None and remaining:
        # Keep discovery, shortlist membership and diversity unchanged. Only the
        # semantic scorer's representation varies in this isolated experiment.
        texts = [rerank_text(item) if rerank_text else item.text for item, _, _, _ in remaining]
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise ValueError("rerank representation must contain nonempty evidence text")
        semantic = reranker.score(query, texts)
        if len(semantic) != len(remaining) or not all(
            isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) and 0 <= s <= 1
            for s in semantic
        ):
            raise ValueError("reranker must return one finite [0,1] score per candidate")
        remaining = [
            (replace(item, score=s, metadata={**item.metadata, "semantic_score": s}), s, words, overlap)
            for (item, _, words, overlap), s in zip(remaining, semantic)
        ]
    # Exact Jaccard via bitsets, avoiding repeated large set operations.
    vocabulary = {word: i for i, word in enumerate({w for _, _, words, _ in remaining for w in words})}
    remaining = [
        (item, relevance, sum(1 << vocabulary[w] for w in words), overlap)
        for item, relevance, words, overlap in remaining
    ]
    while remaining:

        def utility(entry):
            item, relevance, words, overlap = entry
            repeated = max((sources[ref.source_id] for ref in item.refs), default=0)
            return (
                (1 - config.redundancy_weight) * relevance
                - config.redundancy_weight * overlap
                - config.source_weight * min(repeated, 2)
            )

        best = max(range(len(remaining)), key=lambda i: (utility(remaining[i]), -i))
        item, _, words, _ = remaining.pop(best)
        output.append(item)
        sources.update({ref.source_id for ref in item.refs})
        remaining = [
            (
                other,
                relevance,
                other_words,
                max(overlap, (words & other_words).bit_count() / max(1, (words | other_words).bit_count())),
            )
            for other, relevance, other_words, overlap in remaining
        ]
    return output
