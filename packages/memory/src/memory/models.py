"""Public values. AuthContext must come from a trusted authentication boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol


class MemoryErrorBase(Exception):
    pass


class AccessDenied(MemoryErrorBase):
    pass


class Conflict(MemoryErrorBase):
    pass


class InvalidEvidence(MemoryErrorBase):
    pass


def timestamp(value: str | datetime | None = None) -> str:
    dt = value or datetime.now(timezone.utc)
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class AuthContext:
    tenant: str
    principal: str
    # Injected by the hosting application, never accepted directly from an HTTP body.
    administrator: bool = False


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    revision: int
    start: int
    end: int
    content_hash: str


@dataclass(frozen=True)
class ContextAnchor:
    """Literal context span and its applicability span in one source revision."""

    start: int
    end: int
    scope_start: int
    scope_end: int

    def validate(self, source_length: int):
        if not all(type(v) is int for v in (self.start, self.end, self.scope_start, self.scope_end)):
            raise ValueError("context offsets must be integers")
        if not (0 <= self.start < self.end <= source_length and 0 <= self.scope_start < self.scope_end <= source_length):
            raise ValueError("context offsets outside source")


@dataclass(frozen=True)
class Receipt:
    source_id: str
    revision: int
    job_id: str | None
    content_hash: str
    state: str = "stored"


@dataclass(frozen=True)
class ClaimDraft:
    subject: str
    predicate: str
    value: str
    text: str
    refs: tuple[SourceRef, ...]
    kind: Literal["fact", "decision", "risk", "procedure", "hypothesis"] = "fact"
    assertion_id: str | None = None
    expected_revision: int = 0
    valid_from: str | None = None
    valid_to: str | None = None
    # Only human review or a separate verifier may promote a candidate.
    status: Literal["proposed", "supported", "disputed"] = "proposed"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompileInput:
    source_id: str
    revision: int
    content: str
    content_hash: str
    token_budget: int


class Compiler(Protocol):
    version: str

    def compile(self, source: CompileInput) -> list[ClaimDraft]: ...


class Verifier(Protocol):
    def supports(self, draft: ClaimDraft, source_texts: list[str]) -> bool: ...


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    text: str
    refs: tuple[SourceRef, ...] = ()
    revision: int = 1
    score: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceBundle:
    query: str
    items: tuple[Evidence, ...]
    estimated_tokens: int
    diagnostics: dict

    @property
    def context(self) -> str:
        return "\n\n".join(f"[{item.kind}:{item.id}@{item.revision}] {item.text}" for item in self.items)


@dataclass(frozen=True)
class GraphDelta:
    tenant: str
    project: str
    assertion_id: str
    version: int
    subject: str
    predicate: str
    value: str
    kind: str
    active: bool


class GraphSink(Protocol):
    def apply(self, delta: GraphDelta) -> None: ...
