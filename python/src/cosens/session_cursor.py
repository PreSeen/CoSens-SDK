"""Snapshots of a host Session's items for the transparent wrappers."""

import copy
import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path

from ._config import resolve_service
from .errors import SessionHistoryChanged


def default_state_path():
    return Path.home() / ".cosens" / "state.sqlite3"


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _plain(item):
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    if hasattr(item, "__dict__"):
        return vars(item)
    raise TypeError("Session items must be dictionaries or serializable objects")


@dataclass(frozen=True)
class SessionSnapshot:
    key: str
    session_id: str
    items: tuple[dict, ...]


class CursorStore:
    """Snapshots a host Session's items; it keeps no state of its own.

    ``path`` is accepted so callers pass the same arguments as to ``TurnStore``.
    """

    def __init__(self, path=None, *, principal, service_id=None):
        self._service_id = service_id or resolve_service().identity
        self._principal = _hash(principal)

    async def snapshot(self, session):
        session_id = getattr(session, "session_id", None) or getattr(session, "id", None)
        if not session_id:
            raise ValueError("Session must expose a stable session_id or id")
        incarnation = getattr(session, "session_incarnation", None) or getattr(session, "incarnation", "0")
        items = session.get_items()
        if inspect.isawaitable(items):
            items = await items
        if not isinstance(items, (list, tuple)):
            raise TypeError("Session.get_items() must return a list")
        key = _hash([self._service_id, self._principal, str(session_id), str(incarnation)])
        snapshot = SessionSnapshot(key, str(session_id), tuple(copy.deepcopy(_plain(item)) for item in items))
        message_ids = set()
        for position, item in enumerate(snapshot.items):
            message_id = str(item.get("id") or _hash([key, position, item]))
            if message_id in message_ids:
                raise SessionHistoryChanged(f"Session contains duplicate message ID chat:{message_id}")
            message_ids.add(message_id)
        return snapshot

    def close(self):
        """Nothing to release; kept so owners close every store the same way."""
