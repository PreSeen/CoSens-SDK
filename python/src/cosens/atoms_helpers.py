"""Shared resource and idempotency validation for atomic clients."""

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
