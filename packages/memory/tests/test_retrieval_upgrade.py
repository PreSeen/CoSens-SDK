from dataclasses import replace

import pytest

from memory import AuthContext, MemoryService, SourceRef, Store
from memory.models import Evidence
from memory.retrieval import bm25, lexemes, passage, rank


def evidence(key, text, source=None):
    return Evidence(key, "source", text, (SourceRef(source or key, 1, 100, 100 + len(text), "hash"),))


def test_bm25_rare_terms_outrank_repeated_common_terms():
    docs = ["project project project meeting", "project zephyr release", "project meeting"]
    scores = bm25("project zephyr", docs)
    assert scores[1] > max(scores[0], scores[2])
    assert bm25("unknown", docs) == [0, 0, 0]


def test_passage_is_exact_contiguous_span_with_adjusted_offsets():
    text = (
        ("Routine daily update. " * 35)
        + "The zephyr release was cancelled by Mei after an audit. "
        + "Other notes. " * 30
    )
    item = evidence("a", text)
    out = passage(item, "Why was zephyr cancelled?")
    ref = out.refs[0]
    assert len(out.text) <= 560
    assert "cancelled by Mei" in out.text
    assert out.text == text[ref.start - 100 : ref.end - 100]
    assert ref.content_hash == "hash"
    assert passage(replace(item, kind="fact"), "zephyr") == replace(item, kind="fact")


def test_passage_trailing_newline_and_unbroken_token():
    for text in ("Text.\n" * 200, "x" * 2000):
        out = passage(evidence("x", text), "Text")
        assert out.text
        assert len(out.text) <= 560


def test_rank_fusion_retains_vector_only_evidence_and_rejects_zero_match():
    items = [
        evidence("lex", "zephyr release"),
        evidence("dense", "a launch was postponed"),
        evidence("noise", "cooking"),
    ]
    out = rank("zephyr", items, {"dense": 0.9, "not-authorized": 100})
    assert {item.id for item in out} == {"lex", "dense"}
    assert all(item.metadata["retrieval"] == "bm25_rrf" for item in out)
    assert [x.id for x in rank("zephyr", items, {})] == ["lex"]


def test_semantic_shortlist_is_invariant_to_recreated_assertion_ids():
    from memory.retrieval import RetrievalConfig
    left = replace(evidence('a', 'project owner Alice', 'one'), kind='fact')
    right = replace(evidence('z', 'project owner Brian', 'two'), kind='fact')
    before = rank('project owner', [left, right], {}, RetrievalConfig(rerank_limit=1))
    after = rank('project owner', [replace(left, id='z'), replace(right, id='a')], {},
                 RetrievalConfig(rerank_limit=1))
    assert [x.text for x in before] == [x.text for x in after]
    assert before[0].metadata['shortlist_sha256'] == after[0].metadata['shortlist_sha256']


def test_shortlist_fingerprint_binds_order_source_revision_and_literal_text():
    from memory.retrieval import shortlist_fingerprint
    left, right = evidence('a', 'one'), evidence('b', 'two')
    expected = shortlist_fingerprint([left, right])
    assert expected == shortlist_fingerprint([replace(left, id='random'), right])
    assert expected != shortlist_fingerprint([right, left])
    assert expected != shortlist_fingerprint([replace(left, text='changed'), right])
    assert expected != shortlist_fingerprint([replace(left, refs=(replace(left.refs[0], revision=2),)), right])


def test_source_diversity_does_not_repeat_identical_text():
    items = [
        evidence("a", "zephyr delay because audit failed", "same"),
        evidence("b", "zephyr delay because audit failed", "same"),
        evidence("c", "zephyr release owner Mei", "other"),
    ]
    assert len(rank("zephyr", items, {})) == 2


@pytest.mark.parametrize("policy", ["overlap", "hybrid"])
def test_shared_character_cap_and_live_acl(policy):
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    owner = AuthContext("t", "alice", True)
    reader = AuthContext("t", "bob")
    memory.create_project(owner, "p", "Test")
    memory.set_member(owner, "p", "bob", "reader")
    for i in range(8):
        memory.ingest(owner, "p", f"zephyr release {i}: " + "public review text. " * 80, request_key=str(i))
    private = memory.ingest(owner, "p", "zephyr private secret", request_key="private", readers=("alice",))
    for cap in (1, 100, 500, 2000):
        result = memory.recall(reader, "p", "zephyr release", char_budget=cap, retrieval_policy=policy)
        assert len(result.context) <= cap
        assert result.diagnostics["context_chars"] == len(result.context)
        assert private.source_id not in {r.source_id for x in result.items for r in x.refs}
    store.close()


def test_multilingual_tokens_and_invalid_policy():
    assert "项目" in lexemes("项目负责人是谁")
    assert "the" not in lexemes("the zephyr release")
    with pytest.raises(ValueError):
        MemoryService(None).recall(None, "p", "q", retrieval_policy="magic")


def test_claim_structured_subject_is_searchable_without_rewriting_evidence():
    item = replace(
        evidence("decision", "The launch is delayed."),
        kind="decision",
        metadata={"search_text": "Zephyr launch The launch is delayed."},
    )
    result = rank("Zephyr", [item], {})
    assert len(result) == 1
    assert result[0].text == item.text


def test_chinese_late_query_anchor_is_not_discarded_as_one_long_word():
    text = "例行会议记录。" * 120 + "猎户项目因为审计失败而取消上线。" + "其他记录。" * 100
    result = passage(evidence("chinese", text), "猎户项目为什么取消上线")
    assert "审计失败" in result.text
    assert result.text == text[result.refs[0].start - 100 : result.refs[0].end - 100]
