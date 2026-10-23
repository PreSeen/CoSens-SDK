from dataclasses import replace

import pytest

from memory.models import Evidence, SourceRef
from memory.pool_retrieval import source_preserving_shortlist, candidate_pool_fingerprints
from memory.retrieval import RetrievalConfig


def evidence(i, kind='source', text=None):
    text = text or f'project ownership detail {i}'
    return Evidence(str(i), kind, text, (SourceRef(f's{i}', 1, 0, len(text), f'h{i}'),))


def test_derived_volume_does_not_change_independent_raw_pool_or_reserved_slots():
    raw = [evidence(i) for i in range(8)]
    derived = [evidence(i+100, 'fact') for i in range(100)]
    config = RetrievalConfig(rerank_limit=6)
    a = source_preserving_shortlist('project ownership', raw, {}, config, source_reserve=4)
    b = source_preserving_shortlist('project ownership', raw+derived, {}, config, source_reserve=4)
    assert a.source_pool_sha256 == b.source_pool_sha256
    assert [x.text for x in a.items[:4]] == [x.text for x in b.items[:4]]
    assert len(b.items) == 6 and sum(x.kind == 'source' for x in b.items) == 4
    assert all(x.refs for x in b.items)


def test_duplicate_text_does_not_consume_capacity_and_unused_slots_backfill():
    raw = [evidence(i) for i in range(5)]
    duplicate = replace(raw[0], id='derived', kind='fact')
    result = source_preserving_shortlist('project ownership', raw+[duplicate], {},
        RetrievalConfig(rerank_limit=5), source_reserve=3)
    assert len(result.items) == 5
    assert len({' '.join(x.text.split()).casefold() for x in result.items}) == 5


def test_vectors_are_filtered_to_each_authorized_pool_and_input_order_is_stable():
    items = [evidence(1), evidence(2), evidence(3, 'fact')]
    config = RetrievalConfig(rerank_limit=3)
    scores = {'1': .7, '2': .8, '3': .9, 'foreign': 100}
    a = source_preserving_shortlist('project', items, scores, config, source_reserve=2)
    b = source_preserving_shortlist('project', list(reversed(items)), scores, config, source_reserve=2)
    assert a.shortlist_sha256 == b.shortlist_sha256
    assert {x.id for x in a.items} == {'1', '2', '3'}


@pytest.mark.parametrize('reserve', [0, -1, 4, True, 1.5])
def test_invalid_reserve_is_rejected(reserve):
    with pytest.raises(ValueError):
        source_preserving_shortlist('q', [], {}, RetrievalConfig(rerank_limit=3), source_reserve=reserve)


def test_empty_raw_pool_backfills_from_derived_without_fabricating_sources():
    items = [evidence(i, 'fact') for i in range(5)]
    result = source_preserving_shortlist('project', items, {},
        RetrievalConfig(rerank_limit=3), source_reserve=2)
    assert len(result.items) == 3 and all(x.kind == 'fact' for x in result.items)


def test_reserved_configuration_cannot_silently_skip_common_scoring():
    from memory.retrieval import rank, rerank_shortlist
    with pytest.raises(ValueError, match='requires literal'):
        RetrievalConfig(source_reserve=1)
    config = RetrievalConfig(source_reserve=1, materialize_derived=True, rerank_literal_sources=True)
    with pytest.raises(ValueError, match='independent pools'):
        rank('q', [], {}, config)
    with pytest.raises(ValueError, match='common scorer'):
        rerank_shortlist('q', [], config, None, None)


def test_pool_input_fingerprint_ignores_ids_and_order_but_binds_search_and_vectors():
    a, b = evidence(1), evidence(2, 'fact')
    expected = candidate_pool_fingerprints([a, b], {'1': .4})
    assert expected == candidate_pool_fingerprints([b, replace(a, id='new')], {'new': .4})
    assert expected != candidate_pool_fingerprints([a, b], {'1': .5})
    assert expected != candidate_pool_fingerprints([replace(a, metadata={'search_text': 'changed'}), b], {'1': .4})
    assert expected != candidate_pool_fingerprints([a, a, b], {'1': .4})
