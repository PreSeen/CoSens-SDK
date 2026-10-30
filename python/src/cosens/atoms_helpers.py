"""Validation shared by the atomic HTTP methods."""
from urllib.parse import quote


def path_part(value):
    if not isinstance(value, str) or not value.strip() or value in {'.', '..'}:
        raise ValueError('resource ID must be a nonempty string and not a dot segment')
    return quote(value, safe='')


def stable_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('idempotency key must be a nonempty string of at most 256 characters without control characters')
    return value
