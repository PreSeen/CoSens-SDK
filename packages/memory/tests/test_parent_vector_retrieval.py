from memory import AuthContext, MemoryService, Store
from memory.retrieval import RetrievalConfig


class ParentIndex:
    def __init__(self, *hits):
        self.hits = hits

    def search(self, tenant, project, vector, *, limit):
        return list(self.hits)[:limit]


def test_parent_vector_only_nominates_literal_chunks_then_acl_and_revision_checks_apply():
    db = Store.sqlite()
    db.migrate()
    memory = MemoryService(db)
    owner, reader = AuthContext("t", "owner", True), AuthContext("t", "reader")
    try:
        memory.create_project(owner, "p", "project")
        memory.set_member(owner, "p", "reader", "reader")
        receipt = memory.ingest(owner, "p", "Alpha document carries the sole evidence.",
                                source_id="source", request_key="source", readers=("owner", "reader"))
        config = RetrievalConfig(passage_chars=560)
        result = memory.recall(
            reader, "p", "unmatched query", query_vector=[1.0], parent_vector_index=ParentIndex(("source", .9)),
            parent_vector_limit=1, use_graph=False, use_wiki=False, rank_config=config,
        )
        assert "sole evidence" in result.context
        assert result.diagnostics["parent_vector_hits"] == 1
        memory.set_source_readers(owner, "p", receipt.source_id, ("owner",))
        denied = memory.recall(
            reader, "p", "unmatched query", query_vector=[1.0], parent_vector_index=ParentIndex(("source", .9)),
            parent_vector_limit=1, use_graph=False, use_wiki=False, rank_config=config,
        )
        assert not denied.items
        memory.set_source_readers(owner, "p", receipt.source_id, ("owner", "reader"))
        memory.ingest(owner, "p", "replacement text", source_id="source", request_key="replacement",
                      expected_revision=1, readers=("owner", "reader"))
        stale = memory.recall(
            reader, "p", "unmatched query", query_vector=[1.0], parent_vector_index=ParentIndex(("source", .9)),
            parent_vector_limit=1, use_graph=False, use_wiki=False, rank_config=config,
        )
        assert "Alpha document" not in stale.context
        assert all(item.refs[0].revision == 2 for item in stale.items)
    finally:
        db.close()


def test_bounded_parent_expansion_never_broadcasts_parent_score_to_unanchored_chunks():
    db = Store.sqlite()
    db.migrate()
    memory = MemoryService(db)
    owner = AuthContext("t", "owner", True)
    try:
        memory.create_project(owner, "p", "project")
        # Fixed windows begin at 0 and 800: only the first window contains
        # alpha, while the parent document as a whole is a vector hit.
        text = "alpha " + "a" * 995 + "BRAVO_UNRELATED " + "b" * 1000
        memory.ingest(owner, "p", text, source_id="source", request_key="source")
        result = memory.recall(
            owner, "p", "alpha", query_vector=[1.0], parent_vector_index=ParentIndex(("source", .9)),
            parent_vector_limit=1, parent_vector_chunk_limit=1, use_graph=False, use_wiki=False,
            rank_config=RetrievalConfig(passage_chars=None),
        )
        assert "alpha" in result.context
        assert "BRAVO_UNRELATED" not in result.context
        assert result.diagnostics["parent_vector_chunks_added"] == 0
    finally:
        db.close()
