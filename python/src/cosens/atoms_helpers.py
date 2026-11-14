"""Shared resource and idempotency validation for atomic clients."""

import hashlib
import json
from urllib.parse import quote


def path_part(value):
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("resource ID must be a nonempty safe path segment")
    return quote(value, safe="")


def stable_key(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 256 or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("key must be 1–256 characters without control characters")
    return value


def recipe_request_key(task_id, revision_id, max_completion_tokens=None, *, web_search=None, web_search_blocked_domains=()):
    """Deterministic generation key: one candidate per revision, completion budget and web search setting.

    TypeScript computes the same key, so retries from either SDK reuse one candidate.
    """
    budget = "" if max_completion_tokens is None else str(max_completion_tokens)
    web = ""
    if web_search is not None or web_search_blocked_domains:
        web = ":web=" + json.dumps([web_search, list(web_search_blocked_domains)], separators=(",", ":"))
    digest = hashlib.sha256(f"recipe:{task_id}:{revision_id}:{budget}{web}".encode("utf-8")).hexdigest()
    return "recipe-" + digest
