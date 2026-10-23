"""Deterministic, Wiki-first routing that never needs a graph-extraction LLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class MemoryLayer(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"
    PROJECT = "project"


PROJECT_EVENT_KINDS = frozenset({
    "decision", "task", "owner", "dependency", "milestone", "status",
    "constraint", "risk", "requirement", "supersession",
})


@dataclass(frozen=True)
class IngestRoute:
    """Host-provided, non-LLM routing signals for one source revision."""

    source_kind: str = "message"
    durable: bool = False
    project_event_kind: str | None = None
    force_layer: str | None = None
    # Optional host-identified event/thread key. It is navigation metadata only;
    # it does not assert that two sources support the same claim.
    event_id: str | None = None

    def payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RouteDecision:
    layer: MemoryLayer
    enqueue_compile: bool
    enqueue_wiki: bool
    project_graph_eligible: bool
    reason: str
    event_id: str | None = None

    def payload(self) -> dict:
        return {
            "layer": self.layer.value,
            "enqueue_compile": self.enqueue_compile,
            "enqueue_wiki": self.enqueue_wiki,
            "project_graph_eligible": self.project_graph_eligible,
            "reason": self.reason,
            "requires_graph_extraction": False,
            "event_id": self.event_id,
        }


def route_ingest(route: IngestRoute) -> RouteDecision:
    if route.event_id is not None and (
        not isinstance(route.event_id, str) or not route.event_id.strip() or len(route.event_id) > 256
    ):
        raise ValueError("event_id must be nonempty text within 256 characters")
    forced = str(route.force_layer or "").strip().lower()
    if forced in {MemoryLayer.HOT.value, MemoryLayer.WARM.value, MemoryLayer.COLD.value}:
        layer = MemoryLayer(forced)
        return RouteDecision(layer, layer is MemoryLayer.WARM, layer is MemoryLayer.WARM, False,
                             "explicit_layer", route.event_id)
    event = str(route.project_event_kind or "").strip().lower()
    if event in PROJECT_EVENT_KINDS:
        return RouteDecision(MemoryLayer.PROJECT, True, True, True, f"project_event:{event}", route.event_id)
    if route.durable:
        return RouteDecision(MemoryLayer.WARM, True, True, False, "durable_source", route.event_id)
    if str(route.source_kind).strip().lower() in {"document", "archive", "transcript", "attachment"}:
        return RouteDecision(MemoryLayer.COLD, False, False, False, "archival_source", route.event_id)
    return RouteDecision(MemoryLayer.HOT, False, False, False, "recent_message", route.event_id)
