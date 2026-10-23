from dataclasses import replace

import pytest

from memory import AuthContext, ClaimDraft, MemoryService, SourceRef, Store
from memory.retrieval import RetrievalConfig


@pytest.fixture
def memory_case():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store, clock=lambda: "2026-01-01T00:00:00+00:00")
    actor = AuthContext("t", "owner", True)
    memory.create_project(actor, "p", "project")
    text = "Orion OWNER Alice. Approval remains conditional on review."
    receipt = memory.ingest(actor, "p", text, request_key="original")
    ref = SourceRef(receipt.source_id, receipt.revision, 0, len(text), receipt.content_hash)
    draft = ClaimDraft(
        subject="Orion", predicate="OWNER", value="Alice", text="Orion custodian Alice", refs=(ref,), status="supported"
    )
    claim = memory.commit(actor, "p", [draft])[0]
    yield memory, actor, receipt, draft, claim, text
    store.close()


def recall(memory, actor, **kwargs):
    return memory.recall(
        actor, "p", "custodian", use_wiki=False, rank_config=RetrievalConfig(materialize_derived=True), **kwargs
    )


@pytest.mark.parametrize("newer_count", [4999, 5000])
def test_supported_source_survives_more_than_5000_newer_claims(memory_case, newer_count):
    memory, actor, receipt, draft, claim, text = memory_case
    assert memory.recall(actor, "p", "Orion", use_wiki=False).items
    memory.clock = lambda: "2026-01-02T00:00:00+00:00"
    for start in range(0, newer_count, 100):
        memory.commit(actor, "p", [
            replace(draft, subject=f"Unrelated-{i}", text=f"Unrelated fact {i}")
            for i in range(start, min(start + 100, newer_count))
        ])
    result = memory.recall(actor, "p", "Orion", use_wiki=False)
    assert any(item.text == text for item in result.items)
    assert any(row["id"] == claim["id"] for row in memory.list_claims(actor, "p"))
    memory.revoke(actor, "p", claim["id"])
    assert not memory.recall(actor, "p", "Orion", use_wiki=False).items


def test_navigation_uses_derived_signal_but_returns_literal_evidence(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case
    result = recall(memory, actor)
    assert result.items and all(x.kind == "source" for x in result.items)
    assert result.items[0].text == text
    assert "custodian" not in result.context
    assert result.items[0].refs == draft.refs
    assert result.items[0].metadata["navigation_origin"] == claim["id"]
    assert result.items[0].metadata["navigation_text"] == claim["text"]
    assert not recall(memory, actor, char_budget=10).items
    assert memory.recall(actor, "p", "custodian", use_wiki=False).items[0].kind == "fact"


def test_entity_fact_inventory_uses_subject_index_but_returns_literal_evidence(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case

    result = memory.entity_fact_inventory(actor, "p", ["Orion"])

    assert [item.text for item in result.items] == [text]
    assert all(item.kind == "source" for item in result.items)
    assert result.items[0].refs == draft.refs
    assert result.items[0].metadata["navigation"] == "entity_fact_inventory"
    # The object is not a subject merely because it occurs in the claim value.
    assert not memory.entity_fact_inventory(actor, "p", ["Alice"]).items
    memory.revoke(actor, "p", claim["id"])
    assert not memory.entity_fact_inventory(actor, "p", ["Orion"]).items


def test_entity_inventory_matches_each_requested_entity_not_the_union(memory_case):
    memory, actor, *_ = memory_case
    assert memory.entity_fact_inventory(actor, "p", ["Orion", "Another Project"]).items


def test_entity_speaker_inventory_returns_primary_source_without_compiler_claim(memory_case):
    memory, actor, *_ = memory_case
    text = ("DATE OF CONVERSATION: 9:00 am on 1 January, 2026\n"
            "Alice: I am going swimming after work.\n"
            "DATE OF CONVERSATION: 9:01 am on 1 January, 2026\n"
            "Bob: Have fun!")
    receipt = memory.ingest(actor, "p", text, request_key="dialogue", source_id="dialogue")

    result = memory.entity_speaker_inventory(actor, "p", ["Alice"])

    assert len(result.items) == 1
    assert result.items[0].kind == "source"
    assert result.items[0].text == text.split("\nDATE", 1)[0]
    assert result.items[0].refs == (SourceRef(receipt.source_id, receipt.revision, 0,
                                                len(result.items[0].text), receipt.content_hash),)
    assert result.items[0].metadata["navigation"] == "entity_speaker_inventory"
    assert not memory.entity_speaker_inventory(actor, "p", ["Bob"]).items == ()


def test_speaker_inventory_keeps_attachment_with_owning_turn(memory_case):
    memory, actor, *_ = memory_case
    text = ("DATE OF CONVERSATION: 1 January 2026\nAlice: I made this.\n"
            "Attachment description (image caption): a fox cup\n"
            "DATE OF CONVERSATION: 2 January 2026\nBob: Nice!")
    memory.ingest(actor, "p", text, request_key="caption", source_id="caption")
    rows = memory.entity_speaker_inventory(actor, "p", ["Alice"]).items
    assert len(rows) == 1
    assert "a fox cup" in rows[0].text
    assert "Bob: Nice!" not in rows[0].text


def test_entity_speaker_inventory_respects_live_source_access(memory_case):
    memory, actor, *_ = memory_case
    text = "DATE OF CONVERSATION: 9:00 am on 1 January, 2026\nAlice: I am going swimming."
    receipt = memory.ingest(actor, "p", text, request_key="private-dialogue", source_id="private-dialogue")
    reader = AuthContext("t", "reader")
    memory.set_member(actor, "p", "reader", "reader")
    assert memory.entity_speaker_inventory(reader, "p", ["Alice"]).items
    memory.set_source_readers(actor, "p", receipt.source_id, (actor.principal,))
    assert not memory.entity_speaker_inventory(reader, "p", ["Alice"]).items


@pytest.mark.parametrize("change", ["acl", "delete", "revoke", "overwrite"])
def test_navigation_cannot_bypass_live_source_or_assertion_constraints(memory_case, change):
    memory, actor, receipt, draft, claim, text = memory_case
    if change == "acl":
        reader = AuthContext("t", "reader")
        memory.set_member(actor, "p", "reader", "reader")
        assert recall(memory, reader).items
        memory.set_source_readers(actor, "p", receipt.source_id, (actor.principal,))
        actor = reader
    elif change == "delete":
        memory.delete_source(actor, "p", receipt.source_id)
    elif change == "revoke":
        memory.revoke(actor, "p", claim["id"])
    else:
        memory.ingest(
            actor,
            "p",
            "Replacement document",
            request_key="new",
            source_id=receipt.source_id,
            expected_revision=receipt.revision,
        )
    assert not recall(memory, actor).items


def test_navigation_checks_excluded_spans_even_without_lexical_source_seed(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case
    other = memory.commit(actor, "p", [replace(draft, predicate="OTHER", text="Another assertion")])[0]
    memory.revoke(actor, "p", other["id"])
    assert not recall(memory, actor).items


def test_future_and_proposed_claims_cannot_be_navigation_seeds(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case
    assert not recall(memory, actor, valid_at="2025-01-01T00:00:00Z").items
    memory.revoke(actor, "p", claim["id"])
    memory.commit(actor, "p", [replace(draft, status="proposed")])
    assert not recall(memory, actor).items


def test_literal_reranking_scores_materialized_sources_without_changing_shortlist(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case

    class Recorder:
        def __init__(self):
            self.texts = []

        def score(self, query, texts):
            self.texts.extend(texts)
            return [0.7] * len(texts)

    original, literal = Recorder(), Recorder()
    outputs = []
    for enabled, scorer in ((False, original), (True, literal)):
        outputs.append(memory.recall(
            actor, "p", "custodian", use_wiki=False, reranker=scorer,
            rank_config=RetrievalConfig(materialize_derived=True, rerank_literal_sources=enabled),
        ))
    assert original.texts == [draft.text]
    assert literal.texts == [text]
    assert outputs[0].items == outputs[1].items
    assert outputs[0].diagnostics['shortlist_sha256'] == outputs[1].diagnostics['shortlist_sha256']
    assert len(outputs[0].diagnostics['shortlist_sha256']) == 64
    memory.revoke(actor, "p", claim["id"])
    literal.texts.clear()
    assert not memory.recall(
        actor, "p", "custodian", use_wiki=False, reranker=literal,
        rank_config=RetrievalConfig(materialize_derived=True, rerank_literal_sources=True),
    ).items
    assert not literal.texts


def test_literal_reranking_requires_navigation_and_actual_scorer(memory_case):
    with pytest.raises(ValueError, match="requires materialize"):
        RetrievalConfig(rerank_literal_sources=True)
    memory, actor, *_ = memory_case
    with pytest.raises(ValueError, match="requires a reranker"):
        memory.recall(
            actor, "p", "custodian", use_wiki=False,
            rank_config=RetrievalConfig(materialize_derived=True, rerank_literal_sources=True),
        )


def test_literal_reranking_keeps_multi_source_claim_atomic_and_authorized(memory_case):
    memory, actor, receipt, draft, claim, text = memory_case
    second_text = "Alice accepted the Orion assignment after the review."
    second = memory.ingest(actor, "p", second_text, request_key="second")
    second_ref = SourceRef(second.source_id, second.revision, 0, len(second_text), second.content_hash)
    memory.commit(actor, "p", [replace(draft, predicate="ASSIGNMENT",
        text="Orion coordinator Alice", refs=(*draft.refs, second_ref))])
    reader = AuthContext("t", "reader")
    memory.set_member(actor, "p", "reader", "reader")
    calls = []

    class Recorder:
        def score(self, query, texts):
            calls.append(texts)
            return [0.9] * len(texts)

    def query():
        return memory.recall(reader, "p", "coordinator", use_wiki=False, reranker=Recorder(),
            rank_config=RetrievalConfig(materialize_derived=True, rerank_literal_sources=True))

    bundle = query()
    assert calls == [[text + "\n\n" + second_text]]
    assert {r.source_id for item in bundle.items for r in item.refs} == {receipt.source_id, second.source_id}
    assert all(item.kind == "source" for item in bundle.items)
    assert "coordinator" not in bundle.context
    memory.set_source_readers(actor, "p", second.source_id, (actor.principal,))
    calls.clear()
    assert not query().items
    assert calls == []


def test_wiki_candidate_cap_is_invariant_to_storage_ids():
    def snapshot(reverse):
        store = Store.sqlite()
        store.migrate()
        try:
            memory = MemoryService(store, clock=lambda: '2026-01-01T00:00:00+00:00')
            actor = AuthContext('t', 'owner', True)
            memory.create_project(actor, 'p', 'project')
            receipt = memory.ingest(actor, 'p', 'source', source_id='original', request_key='original')
            ref = SourceRef(receipt.source_id, receipt.revision, 0, 6, receipt.content_hash)
            for i in range(1001):
                memory.edit_wiki(actor, 'p', f'block-{1000-i if reverse else i:04d}',
                    page='facts', text=f'fact {i:04d}', refs=(ref,), expected_revision=0)
            return sorted(b['text'] for b in memory.read_wiki(actor, 'p')['blocks'])
        finally:
            store.close()
    assert snapshot(False) == snapshot(True)


@pytest.mark.parametrize('change', ['acl', 'revoke', 'delete', 'overwrite', 'future'])
def test_reserved_pool_uses_existing_permission_revision_and_time_gates(memory_case, change):
    memory, actor, receipt, draft, claim, text = memory_case
    reader = AuthContext('t', 'reader')
    memory.set_member(actor, 'p', 'reader', 'reader')
    calls = []

    class Scorer:
        def score(self, query, texts):
            calls.extend(texts)
            return [.8] * len(texts)

    def query(**kwargs):
        return memory.recall(reader, 'p', 'Orion', reranker=Scorer(),
            rank_config=RetrievalConfig(rerank_limit=2, materialize_derived=True,
                rerank_literal_sources=True, source_reserve=1), **kwargs)

    result = query()
    assert result.items and all(item.kind == 'source' for item in result.items)
    assert all(value == text for value in calls)
    assert result.diagnostics['pool_selection']['shortlist_sha256'] == result.diagnostics['shortlist_sha256']
    kwargs = {}
    if change == 'acl':
        memory.set_source_readers(actor, 'p', receipt.source_id, (actor.principal,))
    elif change == 'revoke':
        memory.revoke(actor, 'p', claim['id'])
    elif change == 'delete':
        memory.delete_source(actor, 'p', receipt.source_id)
    elif change == 'overwrite':
        memory.ingest(actor, 'p', 'Replacement document', request_key='new',
            source_id=receipt.source_id, expected_revision=receipt.revision)
    else:
        kwargs['valid_at'] = '2025-01-01T00:00:00Z'
    calls.clear()
    assert not query(**kwargs).items
    assert not calls
