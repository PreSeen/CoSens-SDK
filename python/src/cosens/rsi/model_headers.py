"""Bind host-supplied model headers to a Worker without persisting their values."""

import hashlib
import json
import os
import re

from .models import InvalidState

_PREFIX = "RSI_MODEL_HEADERS_"
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]|[^\x00-\xff]")


def credential_scope_id(scope):
    return hashlib.sha256(scope.encode()).hexdigest() if scope is not None else None


def normalize_request_headers(headers):
    normalized = {}
    for name, value in headers.items():
        if not _HEADER_NAME.fullmatch(name) or _BAD_VALUE.search(value):
            raise ValueError("Invalid HTTP request headers")
        name, value = name.lower(), value.strip(" \t")
        if name in normalized:
            normalized[name] += ", " + value
        else:
            normalized[name] = value
    return normalized


def bind_model_headers(base_url, model, headers, *, credential_scope=None):
    normalized = normalize_request_headers(headers)
    identity = [base_url.rstrip("/"), model, sorted(normalized.items())]
    if credential_scope is not None:
        identity.append(credential_scope)
    identity = json.dumps(identity, ensure_ascii=False)
    name = _PREFIX + hashlib.sha256(identity.encode()).hexdigest()
    return name, json.dumps(normalized)


def is_header_binding(name):
    return name.startswith(_PREFIX)


def model_headers(name):
    value = os.environ.get(name)
    if not value:
        raise InvalidState("Model headers are unavailable for this execution")
    return (
        json.loads(value)
        if is_header_binding(name)
        else {"authorization": f"Bearer {value}"}
    )
