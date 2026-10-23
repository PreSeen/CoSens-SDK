from types import SimpleNamespace

import pytest

from memory import AuthContext, ClaimDraft, MemoryService, SourceRef, Store
from memory.retrieval import RetrievalConfig


@pytest.mark.parametrize("value", [-1, 3, 0.5, True])
def test_radius_validated(value):
    with pytest.raises(ValueError):
        RetrievalConfig(dense_neighbor_radius=value)


def test_neighbors_are_bounded_literal_same_source_and_acl_checked():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    owner = AuthContext("t", "owner", True)
    reader = AuthContext("t", "reader")
    memory.create_project(owner, "p", "project")
    memory.set_member(owner, "p", "reader", "reader")
    body = "".join(f"shared{i}" + str(i) * 793 for i in range(7))
    receipt = memory.ingest(owner, "p", body, request_key="a")
    chunks = store.all("SELECT * FROM chunks ORDER BY start_offset", ())
    index = SimpleNamespace(search=lambda *args, **kwargs: [(chunks[3]["id"], 0.9)])
    config = RetrievalConfig(lexical_weight=0, dense_neighbor_radius=1, source_weight=0, redundancy_weight=0)
    result = memory.recall(
        reader, "p", "shared", rank_config=config, vector_index=index, query_vector=[1], token_budget=10000
    )
    assert {item.id for item in result.items} == {chunks[i]["id"] for i in [2, 3, 4]}
    for item in result.items:
        for ref in item.refs:
            assert item.text == body[ref.start : ref.end]
    memory.set_source_readers(owner, "p", receipt.source_id, ("owner",))
    hidden = memory.recall(reader, "p", "unmatched", rank_config=config, vector_index=index, query_vector=[1])
    assert not hidden.items and hidden.diagnostics["neighbor_candidates_added"] == 0
    memory.delete_source(owner, "p", receipt.source_id)
    assert not memory.recall(owner, "p", "unmatched", rank_config=config, vector_index=index, query_vector=[1]).items
    store.close()


def test_revoked_seed_cannot_propagate_to_other_spans():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store, clock=lambda: "2026-01-02T00:00:00+00:00")
    owner = AuthContext("t", "owner", True)
    memory.create_project(owner, "p", "project")
    text = "A" * 2400 + "The owner is Alice." + "B" * 2400
    receipt = memory.ingest(owner, "p", text, request_key="source")
    seed = store.one("SELECT * FROM chunks WHERE start_offset=2400", ())
    index = SimpleNamespace(search=lambda *args, **kwargs: [(seed["id"], 0.9)])
    config = RetrievalConfig(lexical_weight=0, dense_neighbor_radius=2)
    future = memory.recall(
        owner,
        "p",
        "unmatched",
        rank_config=config,
        vector_index=index,
        query_vector=[1],
        valid_at="2026-01-01T00:00:00+00:00",
    )
    assert not future.items and future.diagnostics["neighbor_candidates_added"] == 0
    ref = SourceRef(receipt.source_id, receipt.revision, 2400, 2419, receipt.content_hash)
    claim = memory.commit(
        owner,
        "p",
        [
            ClaimDraft(
                subject="project",
                predicate="owner",
                value="Alice",
                text="The owner is Alice.",
                refs=(ref,),
                status="supported",
            )
        ],
    )[0]
    memory.revoke(owner, "p", claim["id"])
    result = memory.recall(owner, "p", "unmatched", rank_config=config, vector_index=index, query_vector=[1])
    assert not result.items and result.diagnostics["neighbor_candidates_added"] == 0
    store.close()
