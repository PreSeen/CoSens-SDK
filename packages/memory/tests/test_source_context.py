import pytest

from memory import AuthContext, ClaimDraft, Conflict, ContextAnchor, MemoryService, SourceRef, Store
from memory.retrieval import RetrievalConfig


@pytest.fixture
def case():
    db = Store.sqlite()
    db.migrate()
    memory = MemoryService(db, clock=lambda: "2026-01-01T00:00:00+00:00")
    actor = AuthContext("t", "owner", True)
    memory.create_project(actor, "p", "project")
    text = "Document header 2025.\n" + "Unrelated background. " * 80 + "\nOrion moved to Paris."
    anchors = (ContextAnchor(0, 21, 0, len(text)),)
    receipt = memory.ingest(actor, "p", text, request_key="one", source_id="doc", context_anchors=anchors)
    yield memory, actor, text, anchors, receipt
    db.close()


def recall(memory, actor, **kwargs):
    return memory.recall(actor, "p", "Orion Paris", rank_config=RetrievalConfig(source_context=True), **kwargs)


def test_literal_context_atomic_budget_and_default_off(case):
    memory, actor, text, _, _ = case
    bundle = recall(memory, actor)
    contexts = [r for r in bundle.items if r.kind == "source_context"]
    assert contexts
    for item in bundle.items:
        ref = item.refs[0]
        assert item.text == text[ref.start:ref.end]
    assert len(bundle.context) == bundle.diagnostics["context_chars"]
    assert not any(r.kind == "source_context" for r in memory.recall(actor, "p", "Orion Paris").items)
    assert not recall(memory, actor, char_budget=100).items


def test_anchor_idempotency_and_bounds(case):
    memory, actor, text, anchors, receipt = case
    assert memory.ingest(actor, "p", text, request_key="one", source_id="doc", context_anchors=anchors) == receipt
    with pytest.raises(Conflict):
        memory.ingest(actor, "p", text, request_key="one", source_id="doc")
    for anchor in (ContextAnchor(-1, 3, 0, 4), ContextAnchor(True, 3, 0, 4), ContextAnchor(0, 3, 0, len(text)+1)):
        with pytest.raises(ValueError):
            memory.ingest(actor, "p", text, request_key="bad", context_anchors=(anchor,))


@pytest.mark.parametrize("change", ["acl", "delete", "revise", "revoke_context"])
def test_context_does_not_bypass_source_constraints(case, change):
    memory, actor, text, _, receipt = case
    assert recall(memory, actor).items
    if change == "acl":
        reader = AuthContext("t", "reader")
        memory.set_member(actor, "p", "reader", "reader")
        memory.set_source_readers(actor, "p", "doc", (actor.principal,))
        assert not recall(memory, reader).items
    elif change == "delete":
        memory.delete_source(actor, "p", "doc")
        assert not recall(memory, actor).items
        assert not memory.db.all("SELECT * FROM source_context")
    elif change == "revise":
        memory.ingest(actor, "p", "Orion is in Rome.", request_key="two", source_id="doc", expected_revision=1)
        assert not any(r.kind == "source_context" for r in recall(memory, actor).items)
    else:
        ref = SourceRef("doc", 1, 0, 21, receipt.content_hash)
        result = memory.commit(actor, "p", [ClaimDraft(subject="document", predicate="TITLE", value="header",
                              text=text[:21], refs=(ref,), status="supported")])[0]
        memory.revoke(actor, "p", result["id"])
        assert not recall(memory, actor).items


def test_context_historical_revision_and_future_cutoff(case):
    memory, actor, text, _, _ = case
    memory.clock = lambda: "2026-02-01T00:00:00+00:00"
    new = text.replace("2025", "2026")
    memory.ingest(actor, "p", new, request_key="new", source_id="doc", expected_revision=1,
                  context_anchors=(ContextAnchor(0, 21, 0, len(new)),))
    before = recall(memory, actor, known_at="2025-12-01T00:00:00+00:00")
    historic = recall(memory, actor, known_at="2026-01-15T00:00:00+00:00")
    current = recall(memory, actor)
    assert not before.items
    assert historic.items and all(r.revision == 1 for r in historic.items)
    assert current.items and all(r.revision == 2 for r in current.items)
    assert any("2025" in r.text for r in historic.items if r.kind == "source_context")
    assert any("2026" in r.text for r in current.items if r.kind == "source_context")


def test_context_not_attached_across_applicability_boundary(case):
    memory, actor, text, _, _ = case
    # This anchor's applicability ends before the distant relevant passage.
    memory.ingest(actor, "p", text, request_key="scope", source_id="doc", expected_revision=1,
                  context_anchors=(ContextAnchor(0, 21, 0, 500),))
    bundle = recall(memory, actor)
    assert bundle.items
    assert not any(r.kind == "source_context" for r in bundle.items)
