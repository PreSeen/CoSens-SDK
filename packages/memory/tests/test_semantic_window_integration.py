from dataclasses import replace

import pytest

from memory import AuthContext, ClaimDraft, MemoryService, SourceRef, Store
from memory.retrieval import RetrievalConfig, rank
from memory.models import Evidence


class Scorer:
    def __init__(self):
        self.calls = []

    def score(self, query, texts):
        self.calls.append(texts)
        return [.8] * len(texts)


def test_same_parent_selection_and_one_scorer_invocation():
    items = [Evidence(str(i), 'source', 'query '+str(i)+' x'*496,
                      (SourceRef(str(i), 1, 0, 1000, 'h'),), 1) for i in range(4)]
    # Construct exact literal reference lengths, independent of fixture digits.
    items = [replace(x, refs=(replace(x.refs[0], end=len(x.text)),)) for x in items]
    configs = [RetrievalConfig(rerank_limit=3, semantic_windows=flag) for flag in (False, True)]
    scorers = [Scorer(), Scorer()]
    results = [rank('query', items, {}, c, reranker=s) for c, s in zip(configs, scorers)]
    assert {r.id for r in results[0]} == {r.id for r in results[1]}
    assert results[0][0].metadata['parent_shortlist_sha256'] == results[1][0].metadata['parent_shortlist_sha256']
    assert [len(s.calls) for s in scorers] == [1, 1]
    assert [len(s.calls[0]) for s in scorers] == [3, 9]


@pytest.mark.parametrize('change', ['acl', 'delete', 'replace', 'revoke', 'future'])
def test_live_eligibility_filters_full_parents_before_window_scoring(change):
    store = Store.sqlite()
    store.migrate()
    try:
        memory = MemoryService(store, clock=lambda: '2026-01-01T00:00:00+00:00')
        owner, reader = AuthContext('t', 'owner', True), AuthContext('t', 'reader')
        memory.create_project(owner, 'p', 'project')
        memory.set_member(owner, 'p', 'reader', 'reader')
        text = 'query ' + 'x'*994
        receipt = memory.ingest(owner, 'p', text, request_key='source')
        scorer = Scorer()
        config = RetrievalConfig(semantic_windows=True, source_context=True, rerank_limit=30)

        def recall(**kwargs):
            return memory.recall(reader, 'p', 'query', use_graph=False, use_wiki=False,
                                 reranker=scorer, rank_config=config, char_budget=6000, **kwargs)

        before = recall()
        assert before.items and len(scorer.calls) == 1
        assert len(before.context) <= 6000
        if change == 'acl':
            memory.set_source_readers(owner, 'p', receipt.source_id, ('owner',))
        elif change == 'delete':
            memory.delete_source(owner, 'p', receipt.source_id)
        elif change == 'replace':
            memory.ingest(owner, 'p', 'replacement', request_key='replace', source_id=receipt.source_id,
                          expected_revision=receipt.revision)
        elif change == 'revoke':
            ref = SourceRef(receipt.source_id, receipt.revision, 0, len(text), receipt.content_hash)
            claim = memory.commit(owner, 'p', [ClaimDraft(subject='s', predicate='p', value='v',
                text='query fact', refs=(ref,), status='supported')])[0]
            memory.revoke(owner, 'p', claim['id'])
        scorer.calls.clear()
        after = recall(valid_at='2025-01-01T00:00:00Z') if change == 'future' else recall()
        assert not after.items and not scorer.calls
    finally:
        store.close()


def test_incompatible_feature_combinations_fail_explicitly():
    with pytest.raises(ValueError):
        RetrievalConfig(semantic_windows=True, materialize_derived=True)
    with pytest.raises(ValueError):
        RetrievalConfig(semantic_windows=1)
    with pytest.raises(ValueError):
        rank('query', [], {}, RetrievalConfig(semantic_windows=True))
