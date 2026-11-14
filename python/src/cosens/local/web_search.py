"""Web search for local Recipe generation: the caller's search, the domain controls and the frozen sources."""

import hashlib
import time
from typing import Protocol, Sequence
from urllib.parse import urlsplit

MAX_BLOCKED_DOMAINS = 50
MAX_QUERIES = 3
RESULTS_PER_QUERY = 5
EXCERPT_CHARS = 1500
TOTAL_CHARS = 24000


class WebSearchUnavailable(RuntimeError):
    """Raise from a ``WebSearch`` that cannot answer. Generation then fails with ``web_search_unavailable``."""

    def __init__(self, message="web search did not answer", *, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class WebSearch(Protocol):
    """The caller's search provider. ``blocked_domains`` is passed so the provider can exclude them; the SDK also drops
    any result on those hosts. Return dicts with ``url``, ``title`` and ``text`` (an excerpt)."""

    async def search(self, query: str, *, blocked_domains: Sequence[str]) -> list: ...


def validate_blocked_domains(values):
    """Bare hostnames only: no scheme, path, port or credentials."""
    values = list(values)
    if len(values) > MAX_BLOCKED_DOMAINS:
        raise ValueError(f"at most {MAX_BLOCKED_DOMAINS} blocked domains")
    for value in values:
        parts = urlsplit("//" + value) if isinstance(value, str) else None
        if (parts is None or parts.hostname != value.lower() or parts.port is not None or "." not in value
                or parts.path or parts.query):
            raise ValueError("blocked domains must be bare hostnames such as example.com")
    return [value.lower() for value in values]


def host_matches(url, domains):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def freeze_sources(queries, found, blocked):
    """The record a candidate keeps: sources the generator may read, and results dropped by host."""
    sources, dropped, seen, size = [], [], set(), 0
    for query, results in zip(queries, found):
        for item in results[:RESULTS_PER_QUERY]:
            url, text = item["url"], str(item.get("text") or "")[:EXCERPT_CHARS]
            if url in seen:
                continue
            seen.add(url)
            if host_matches(url, blocked):
                dropped.append({"url": url, "reason": "blocked_host"})
            elif size + len(text) <= TOTAL_CHARS:
                size += len(text)
                sources.append({"id": f"web_{len(sources) + 1}", "query": query, "url": url,
                                "title": str(item.get("title") or ""), "text": text,
                                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
    return {"status": "completed", "queries": list(queries), "sources": sources, "dropped": dropped}
