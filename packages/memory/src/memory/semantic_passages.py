"""Experimental semantic selection of literal windows, opt-in only.

Caller supplies freshly authorized full source chunks, then uses the existing
context/provenance packer. No store access, source expansion, generated facts,
gold annotations or per-benchmark routing. Scoring costs grow with windows.
"""

import math
from dataclasses import replace


def select_passages(query, candidates, scorer, *, max_chars=560, max_windows=200):
    """Score bounded overlapping windows; return one literal window per input.

Input order and parent identity are preserved. One scorer invocation covers
every window. Invalid input/output fails closed before returning any evidence.
Authorization is a caller obligation, never cached by this module. max_windows
is a hard total budget, not permission to silently omit parents or windows.
"""
    if not isinstance(query, str) or not query.strip():
        raise ValueError('nonempty query required')
    if type(max_chars) is not int or max_chars < 2 or type(max_windows) is not int or max_windows < 1:
        raise ValueError('positive integer window budgets required')
    windows, parents = [], []
    for item in candidates:
        if item.kind != 'source' or len(item.refs) != 1 or not item.text:
            raise ValueError('one literal source reference per nonempty chunk required')
        ref = item.refs[0]
        if ref.end-ref.start != len(item.text) or ref.start < 0 or ref.revision != item.revision:
            raise ValueError('source offsets/revision do not match text')
        length = len(item.text)
        starts = sorted(set(range(0, max(1, length-max_chars+1), max_chars//2)) | {max(0, length-max_chars)})
        group = []
        for start in starts:
            end = min(length, start+max_chars)
            text = item.text[start:end]
            window = replace(item, text=text,
                             refs=(replace(ref, start=ref.start+start, end=ref.start+end),),
                             metadata={**item.metadata, 'semantic_window_offsets': [start, end]})
            group.append(len(windows))
            windows.append(window)
            if len(windows) > max_windows:
                raise ValueError('semantic window budget exceeded')
        parents.append(group)
    if not windows:
        return []
    scores = scorer.score(query, [item.text for item in windows])
    if (not isinstance(scores, (list, tuple)) or len(scores) != len(windows)
            or any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores)):
        raise ValueError('one finite score in [0,1] per window required')
    result = []
    for group in parents:
        # Earlier offset breaks exact ties; neither random IDs nor question
        # vocabulary affect the set of proposed windows.
        best = max(group, key=lambda index: (scores[index], -index))
        window = windows[best]
        result.append(replace(window, score=scores[best], metadata={
            **window.metadata, 'semantic_window_score': scores[best],
            'semantic_window_count': len(group), 'semantic_window_total': len(windows)}))
    return result
