"""Bounded, deterministic navigation from public question text only.

Plans are search hints, never facts, entity identities, or update relations.
Dates describe the user's question and must not be treated as source validity.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date

_DATE = re.compile(
    r"\b(?:as\s+of|perspective\s+of(?:\s+the\s+audit)?(?:\s+on)?|"
    r"standpoint(?:\s+of)?|record\s+on|audit\s+on)\s+"
    r"(?:(20\d{2}-\d{2}-\d{2})|"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})\s*,?\s*(20\d{2}))\b", re.IGNORECASE,
)
_MONTHS = {name.casefold(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"), 1)}
_FOCUS_PATTERNS = (
    # A conclusion or claim can naturally take a "that" clause.
    re.compile(r"\b(?:conclusion|claim)\s+(?:about|regarding|concerning|on|that)\s+([^,?;\n]+)", re.I),
    # Keep "facts that have changed" from being mistaken for the topic.
    re.compile(r"\bfacts?\s+(?:on\s+record\s+)?(?:about|regarding|concerning|on)\s+([^,?;\n]+)", re.I),
    # Common audit-question wording places the topic after an auxiliary clause.
    re.compile(r"\b(?:what|which)\s+(?:facts|data)\s+did\s+the\s+system\s+(?:hold|record|contain)\s+(?:about|regarding|concerning|on)\s+([^,?;\n]+)", re.I),
    re.compile(r"\bfacts?\s+(?:the\s+system\s+|it\s+)?(?:held|recorded|contained)\s+(?:about|regarding|concerning|on)\s+([^,?;\n]+)", re.I),
)
_CHANGES = re.compile(
    r"\b(?:after|thereafter|since|subsequen\w*|later|replacement|replaced|superseded|"
    r"chang\w*|modified|revis\w*|alter\w*|omit\w*|remov\w*|deleted)\b", re.I,
)
_EVIDENCE = re.compile(r"\b(?:evidence|testimony|inference|underlie|underpin\w*|justify|justification)\b", re.I)


@dataclass(frozen=True)
class QueryPlan:
    focus: str | None
    as_of: str | None
    compare_changes: bool
    asks_for_evidence: bool
    ambiguous_dates: bool
    supplementary_queries: tuple[str, ...]

    def payload(self):
        return asdict(self)


def plan_query(query: str) -> QueryPlan:
    if not isinstance(query, str) or not query.strip() or len(query) > 16384:
        raise ValueError("public query must contain 1..16384 characters")
    dates = []
    invalid = False
    for match in _DATE.finditer(query):
        try:
            value = (date.fromisoformat(match[1]) if match[1] else
                     date(int(match[4]), _MONTHS[match[2].casefold()], int(match[3]))).isoformat()
            if value not in dates:
                dates.append(value)
        except ValueError:
            invalid = True
    ambiguous = len(dates) > 1 or invalid
    cutoff = dates[0] if len(dates) == 1 and not ambiguous else None
    focus = None
    match = None
    for pattern in _FOCUS_PATTERNS:
        match = pattern.search(query)
        if match:
            break
    if match:
        # Keep the user's exact topic wording; remove the temporal/instruction
        # suffix rather than inventing entities or a hypothetical answer.
        candidate = re.split(r"\s+as\s+of\b|\.\s+(?=[A-Z])", match[1], maxsplit=1, flags=re.I)[0].strip()
        if 2 <= len(candidate.split()) and len(candidate) <= 512:
            focus = candidate
    # Preserve coordinated parts of the user's topic. Commas alone are
    # not an instruction boundary (e.g. "retain X, dropping Y").
    patterns = (
        r"\b(?:conclusion|claim)\s+(?:about|regarding|concerning|on|that)\s+(.+)",
        r"\b(?:facts?|data|record)\b.{0,160}?\b(?:about|regarding|concerning)\s+(.+)",
        r"\bRegarding\s+(.+)",
    )
    for pattern in patterns:
        topic = re.search(pattern, query, re.I)
        if not topic:
            continue
        candidate = re.split(
            r"\s+as\s+of\b|[?;\n]|\.\s+(?=[A-Z])|"
            r",\s*(?:(?:and|then)\s+)?(?:walk\b|could\s+you\b|"
            r"please\b|list\b|identify\b|indicat\w*\b|highlight\b|"
            r"specify\b|enumerat\w*\b|state\b|what\b|for\s+each\b|marking\s+each\b)",
            topic[1], maxsplit=1, flags=re.I,
        )[0].strip().rstrip("., ")
        if 2 <= len(candidate.split()) and len(candidate) <= 512:
            focus = candidate
            break
    supplements = (focus,) if focus and focus.casefold() != query.strip().casefold() else ()
    return QueryPlan(focus, cutoff, bool(cutoff and _CHANGES.search(query)),
                     bool(_EVIDENCE.search(query)), ambiguous, supplements)


def source_type_hint(text: str) -> str | None:
    """Infer a document format from literal body structure, never annotations.

    A format hint says nothing about whether testimony is direct or inferred.
    HTML comments are removed so synthetic metadata cannot supply an answer.
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if re.search(r"^From:\s*\S", text, re.M | re.I) and re.search(r"^(?:To|Subject):\s*\S", text, re.M | re.I):
        return "email_thread"
    if re.search(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s+[^\n:]{1,80}:", text, re.M):
        return "meeting_transcript"
    if re.search(r"\[\d{4}-\d{2}-\d{2}[^\]\n]*\]\s*@", text):
        return "slack_thread"
    if re.search(r"^#{1,4}\s+Agenda\b", text, re.M | re.I) and re.search(r"Attendees:", text, re.I):
        return "meeting_notes"
    return None
