from dataclasses import replace

import pytest

from memory import AuthContext, ClaimDraft, IngestRoute, MemoryService, SourceRef, Store
from memory.models import Evidence, EvidenceBundle
from memory.planned_retrieval import merge_pools
from memory.query_plan import plan_query, source_type_hint
from memory.retrieval import RetrievalConfig


@pytest.mark.parametrize("query,expected", [
    ("As of April 2, 2023, list the facts and subsequent changes.", "2023-04-02"),
    ("What was on record as of 2024-02-29?", "2024-02-29"),
    ("What happened on 2023-04-02?", None),
    ("As of 2023-02-29, what was known?", None),
    ("Compare as of 2023-01-01 and as of 2023-02-01.", None),
])
def test_public_dates_are_explicit_valid_and_unambiguous(query, expected):
    assert plan_query(query).as_of == expected


def test_focus_is_public_topic_not_a_generated_answer():
    query = "Regarding the conclusion about the Zephyr rollout, list the documents and their testimony."
    plan = plan_query(query)
    assert plan.focus == "the Zephyr rollout"
    assert plan.supplementary_queries == ("the Zephyr rollout",)
    assert plan.asks_for_evidence
    assert not plan.compare_changes
    assert plan_query("List facts about Zephyr as of 2023-01-01 and later replacements.").compare_changes


@pytest.mark.parametrize("query,expected", [
    ("From the perspective of 2023-03-30, what data did the system record concerning the incident with Ethan Brooks and Omar Al-Jabri? Enumerate any facts that have been modified or omitted since that date.",
     "the incident with Ethan Brooks and Omar Al-Jabri"),
    ("From the perspective of the audit on 2023-04-02, what facts did the system hold about the commission structure? Enumerate all facts that have been changed or removed after that date.",
     "the commission structure"),
    ("From the perspective of the system's record on 2023-03-12, enumerate the facts it contained about the investigation of the 30-minute API latency spike, highlight all facts that have changed thereafter.",
     "the investigation of the 30-minute API latency spike"),
    ("From the standpoint of 2023-02-24, outline the facts on record regarding Emily Zhao's role at Helix Logistics. Highlight any facts that have been revised or removed since then, and provide replacements.",
     "Emily Zhao's role at Helix Logistics"),
])
def test_audit_query_focus_skips_followup_change_clause(query, expected):
    plan = plan_query(query)
    assert plan.focus == expected
    assert plan.supplementary_queries == (expected,)
    assert plan.compare_changes


@pytest.mark.parametrize("text,expected", [
    ("From: Ali\nTo: Bea\nSubject: Plan\nBody", "email_thread"),
    ("[09:15:00] Ali: Ship tomorrow", "meeting_transcript"),
    ("[2023-02-01 09:15] @Ali: Ship tomorrow", "slack_thread"),
    ("**Attendees:** Ali, Bea\n## Agenda\nPlan", "meeting_notes"),
    ("<!-- genre: meeting_notes testimony_type: inferred -->\nA plain note.", None),
])
def test_document_format_hints_use_body_not_synthetic_testimony_metadata(text, expected):
    assert source_type_hint(text) == expected


def item(source, n=0, *, revision=1):
    text = f"literal {source} {n}"
    return Evidence(f"{source}:{n}:{revision}", "source", text,
                    (SourceRef(source, revision, n * 100, n * 100 + len(text), f"hash-{revision}"),), revision)


def test_source_rrf_counts_document_once_and_preserves_distinct_spans_and_revisions():
    a, a1, b = item("a"), item("a", 1), item("b")
    pool = merge_pools("query", [EvidenceBundle("q", (a, a1, b), 0, {}),
                                 EvidenceBundle("q", (b, a, replace(a, revision=2,
                                     refs=(replace(a.refs[0], revision=2, content_hash="hash-2"),))), 0, {})])
    assert len(pool.items) == 4
    assert pool.items[0].score == pool.items[1].score
    assert {e.refs[0].source_id for e in pool.items[:2]} == {"a", "b"}
    assert {e.revision for e in pool.items} == {1, 2}


@pytest.fixture
def memory_case():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    owner = AuthContext("tenant", "owner", True)
    memory.create_project(owner, "project", "Project")
    receipt = memory.ingest(owner, "project", "Zephyr rollout is approved by Ali.",
                            source_id="one", request_key="one", route=IngestRoute())
    yield memory, owner, receipt
    store.close()


@pytest.mark.parametrize("change", ["acl", "delete", "revoke", "revision"])
def test_all_passes_revalidate_live_permissions_and_source_versions(memory_case, monkeypatch, change):
    memory, owner, receipt = memory_case
    reader = AuthContext("tenant", "reader")
    memory.set_member(owner, "project", "reader", "reader")
    original = memory.recall
    count = 0

    def mutating_recall(*args, **kwargs):
        nonlocal count
        result = original(*args, **kwargs)
        count += 1
        if count == 2:
            if change == "acl":
                memory.set_source_readers(owner, "project", "one", ("owner",))
            elif change == "delete":
                memory.delete_source(owner, "project", "one")
            elif change == "revision":
                memory.ingest(owner, "project", "Replacement unrelated record.", request_key="two",
                              source_id="one", expected_revision=1, route=IngestRoute())
            else:
                text = "Zephyr rollout is approved by Ali."
                claim = memory.commit(owner, "project", [ClaimDraft("Zephyr", "APPROVAL", "Ali", text,
                    (SourceRef("one", 1, 0, len(text), receipt.content_hash),), status="supported")])[0]
                memory.revoke(owner, "project", claim["id"])
        return result

    monkeypatch.setattr(memory, "recall", mutating_recall)
    result = memory.recall_planned(reader, "project", "Show the evidence for the conclusion about Zephyr rollout.")
    assert count == 2
    assert not result.items
    assert result.diagnostics["live_pool_items_removed"] > 0


def test_focused_pass_never_reuses_original_query_vector_or_calls_reranker(memory_case, monkeypatch):
    memory, owner, _ = memory_case
    seen = []
    original = memory.recall

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, "recall", spy)
    # A supplied vector without an index has no external effect in base recall.
    result = memory.recall_planned(owner, "project", "Explain the conclusion about Zephyr rollout.",
                                   query_vector=(1.0,))
    assert result.items and len(seen) == 2
    assert seen[0]["query_vector"] == (1.0,)
    assert "query_vector" not in seen[1]
    assert result.diagnostics["planning_model_calls"] == 0


def test_budget_historical_questions_and_missing_evidence_are_conservative(memory_case):
    memory, owner, _ = memory_case
    query = "List facts about Zephyr rollout as of 2023-01-01 and subsequent changes."
    result = memory.recall_planned(owner, "project", query)
    assert result.items  # Question cutoff is presentation, not a ledger validity filter.
    assert result.diagnostics["query_plan"]["as_of"] == "2023-01-01"
    assert len(result.context) <= 6000
    assert not memory.recall_planned(owner, "project", query, char_budget=1).items
    assert not memory.recall_planned(owner, "project", "Who owns the missing Kestrel initiative?").items
    with pytest.raises(ValueError, match="literal materialization"):
        memory.recall_planned(owner, "project", query, use_graph=True)
    with pytest.raises(ValueError, match="literal materialization"):
        memory.recall_planned(owner, "project", query, rank_config=RetrievalConfig(source_context=True))
