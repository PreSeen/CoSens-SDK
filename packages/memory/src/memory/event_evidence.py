"""Query-time expansion of recalled event groups with literal sibling evidence.

Thread/group membership is a navigation hint, not a support relation. The
caller supplies currently eligible source metadata and must revalidate the
returned spans before rendering them to a reader.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import replace

from .models import Evidence, SourceRef
from .retrieval import bm25, passage


_TURN = re.compile(
    r"(?m)^(?!(?:From|To|Cc|Subject|Date|Status|Severity|Started|Resolved|Decision|Action|Owner|Note):)"
    r"(?:\[\d{1,4}[^\]\n]*\]\s*[^\n:]{1,100}:|"
    r"[A-Z][\w .'-]{1,60}:\s)(?!//)"
)


def _sentences(text: str):
    """Yield sentence-like spans with original character offsets."""
    boundary = re.compile(r"(?<=[.!?。！？])\s+|\n+")
    start = 0
    for match in boundary.finditer(text):
        end = match.start()
        if text[start:end].strip():
            left = start + len(text[start:end]) - len(text[start:end].lstrip())
            right = start + len(text[start:end].rstrip())
            yield left, right
        start = match.end()
    if text[start:].strip():
        left = start + len(text[start:]) - len(text[start:].lstrip())
        right = start + len(text[start:].rstrip())
        yield left, right


def _support_window(whole: Evidence, query: str, spans, anchor: int, max_chars: int) -> Evidence:
    """Expand an anchor in the original source, preserving its speaker turn.

    Neighbouring complete sentences recover antecedents, qualifications and
    reasons. A different labelled speaker is a hard boundary. Very long single
    sentences retain the existing bounded passage fallback and record clipping.
    """
    text, ref = whole.text, whole.refs[0]
    start, end = spans[anchor]
    turns = [match.start() for match in _TURN.finditer(text)]
    scope_start = max((pos for pos in turns if pos <= start), default=0)
    scope_end = min((pos for pos in turns if pos > start), default=len(text))
    # The matched sentence may begin after its speaker label on the same line.
    line_start = text.rfind("\n", scope_start, start) + 1
    line_start = max(scope_start, line_start)
    if end - line_start <= max_chars:
        start = line_start
    clipped = end - start > max_chars
    if not clipped:
        left, right = anchor - 1, anchor + 1
        while True:
            advanced = False
            if left >= 0 and spans[left][0] >= scope_start and end - spans[left][0] <= max_chars:
                start = spans[left][0]
                left -= 1
                advanced = True
            if right < len(spans) and spans[right][1] <= scope_end and spans[right][1] - start <= max_chars:
                end = spans[right][1]
                right += 1
                advanced = True
            if not advanced:
                break
    literal = replace(
        whole, text=text[start:end], refs=(replace(ref, start=ref.start + start, end=ref.start + end),),
        metadata={**whole.metadata, "support_window_policy": "adjacent-source-sentences-v1",
                  "support_anchor": [ref.start + spans[anchor][0], ref.start + spans[anchor][1]],
                  "support_scope": [ref.start + scope_start, ref.start + scope_end],
                  "anchor_clipped": clipped},
    )
    return passage(literal, query, max_chars) if clipped else literal


def sibling_evidence(
    query: str,
    recalled: tuple[Evidence, ...] | list[Evidence],
    source_texts: dict[str, str],
    source_metadata: dict[str, dict],
    source_versions: dict[str, tuple[int, str]],
    *,
    max_items: int = 16,
    max_groups: int = 0,
    max_chars: int = 8000,
    passage_chars: int = 560,
) -> tuple[Evidence, ...]:
    """Find query-relevant literal spans in siblings of already recalled groups.

    No group is discovered from gold labels. A group is considered only after
    one of its members was independently recalled. At most one source span is
    returned per sibling source; exact offsets and content hashes are retained.
    """
    if (type(max_items) is not int or max_items < 0 or type(max_groups) is not int or max_groups < 0
            or type(max_chars) is not int or max_chars < 0):
        raise ValueError("event evidence limits must be nonnegative integers")
    if type(passage_chars) is not int or passage_chars < 1:
        raise ValueError("passage_chars must be a positive integer")
    if max_items == 0 or max_chars == 0:
        return ()

    recalled_sources = set()
    group_strength: dict[str, float] = {}
    for rank, item in enumerate(recalled, 1):
        if item.kind != "source" or len(item.refs) != 1:
            continue
        source_id = item.refs[0].source_id
        recalled_sources.add(source_id)
        group = source_metadata.get(source_id, {}).get("thread_id")
        if not group:
            continue
        strength = float(item.score) if item.score > 0 else 1.0 / rank
        group_strength[group] = max(group_strength.get(group, 0.0), strength)
    if not group_strength:
        return ()
    if max_groups:
        # Expand only groups whose sources ranked highest in the independent
        # retrieval pass. Group membership itself remains a navigation hint.
        ranked_groups = sorted(group_strength, key=lambda group: (-group_strength[group], group))
        group_strength = {group: group_strength[group] for group in ranked_groups[:max_groups]}

    candidates = []
    for source_id, metadata in source_metadata.items():
        group = metadata.get("thread_id")
        text = source_texts.get(source_id)
        version = source_versions.get(source_id)
        if source_id in recalled_sources or group not in group_strength or not text or not version:
            continue
        revision, content_hash = version
        ref = SourceRef(source_id, revision, 0, len(text), content_hash)
        whole = Evidence(
            id="event-source:" + hashlib.sha256(
                json.dumps([source_id, revision, content_hash], separators=(",", ":")).encode()
            ).hexdigest(),
            kind="source", text=text, refs=(ref,), revision=revision,
            metadata={"navigation": "recalled_event_sibling", "thread_id": group},
        )
        sentence_spans = list(_sentences(text))
        if not sentence_spans:
            continue
        sentence_texts = [text[start:end] for start, end in sentence_spans]
        scores = bm25(query, sentence_texts)
        best_idx = max(range(len(scores)), key=lambda i: (scores[i], -sentence_spans[i][0]))
        if scores[best_idx] <= 0:
            continue
        start, end = sentence_spans[best_idx]
        sentence_item = replace(whole, text=text[start:end], refs=(replace(ref, start=start, end=end),))
        selected = passage(sentence_item, query, passage_chars)
        rendered_chars = len(selected.text) + len(str(metadata.get("slot_id") or source_id)) + 96
        if rendered_chars > max_chars:
            continue
        candidates.append((
            selected,
            str(group),
            scores[best_idx] * group_strength[group],
            rendered_chars,
        ))

    chosen: list[Evidence] = []
    used = 0
    chosen_groups = Counter()
    remaining = list(candidates)
    while remaining and len(chosen) < max_items:
        feasible = [row for row in remaining if used + row[3] + (2 if chosen else 0) <= max_chars]
        if not feasible:
            break
        # Prefer a new event group on near-ties, while relevance and the
        # independently recalled seed strength remain the primary signal.
        best = max(feasible, key=lambda row: (
            row[2] / (1 + 0.25 * chosen_groups[row[1]]),
            -chosen_groups[row[1]],
            row[0].text,
        ))
        remaining.remove(best)
        item, group, _, cost = best
        chosen.append(item)
        chosen_groups[group] += 1
        used += cost + (2 if len(chosen) > 1 else 0)
    return tuple(chosen)

