"""Snapshots of a host Session's items for the transparent wrappers."""

import copy
import hashlib
import inspect
import json
import sqlite3
import threading
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
    def __init__(self, path=None, *, principal, service_id=None):
        self._service_id = service_id or resolve_service().identity
        path = str(path or default_state_path())
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS workflow_session_cursors (key TEXT PRIMARY KEY, acked_index INTEGER NOT NULL, "
            "prefix_hash TEXT, session_id TEXT, pending_events TEXT)"
        )
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(workflow_session_cursors)")}
        for column in ("acked_message_id", "acked_item_hash", "source_revision"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE workflow_session_cursors ADD COLUMN {column} TEXT")
        self._db.commit()
        self._principal = _hash(principal)
        self._lock = threading.RLock()

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
        event_ids = set()
        for position in range(len(snapshot.items)):
            event_id = self._event(snapshot, position)["id"]
            if event_id in event_ids:
                raise SessionHistoryChanged(f"Session contains duplicate message ID {event_id}")
            event_ids.add(event_id)
        return snapshot

    @staticmethod
    def _event(snapshot, position):
        item = snapshot.items[position]
        role = str(item.get("role", item.get("type", "unknown")))
        message_id = str(item.get("id") or _hash([snapshot.key, position, item]))
        kind = {"user": "user_prompt", "assistant": "agent_action", "tool": "tool_result"}.get(role, "context_delivery")
        return {
            "id": f"chat:{message_id}",
            "kind": kind,
            "payload": {
                "session_id": snapshot.session_id,
                "message_id": message_id,
                "index": position,
                "role": role,
                "content": item.get("content", item),
                "source_revision": item.get("source_revision"),
            },
        }

    def close(self):
        self._db.close()
