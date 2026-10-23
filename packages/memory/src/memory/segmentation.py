"""Literal, query-independent indexing spans from caller-provided structure.

All structural boundaries are retained, including nested scopes. No generated
text or semantic/role/keyword inference. Whitespace-only cells need no index
entry; the complete immutable source is still stored. Non-whitespace text is
covered. Structural policy uses legacy1000/800 windows locally; units policy
keeps each complete cell, with no silent truncation or oversize fallback. A source
without declared structure is one unit under the explicit units policy.
"""


def index_spans(content, anchors=(), *, policy='fixed'):
    if policy not in {'fixed','structural','units'}:
        raise ValueError('unknown indexing policy')
    if not isinstance(content,str) or not content:
        raise ValueError('nonempty source required')
    anchors=tuple(anchors)
    for anchor in anchors:
        anchor.validate(len(content))
    if policy=='units' and not anchors:
        return ((0,len(content)),)
    if policy=='fixed' or not anchors:
        return tuple((start,min(start+1000,len(content))) for start in range(0,len(content),800))
    boundaries=sorted({0,len(content),*(a.scope_start for a in anchors),*(a.scope_end for a in anchors)})
    spans=[]
    for begin,end in zip(boundaries,boundaries[1:]):
        if policy=='units':
            if content[begin:end].strip():spans.append((begin,end))
            continue
        for start in range(begin,end,800):
            stop=min(start+1000,end)
            if content[start:stop].strip():spans.append((start,stop))
    return tuple(spans)
