import json
from dataclasses import replace

import pytest

from memory import AuthContext, ClaimDraft, ExactQuoteVerifier, MemoryService, SourceRef, Store, Worker
from memory.compiler import OpenAICompatibleCompiler
from memory.models import CompileInput, InvalidEvidence


def parse(content, **kwargs):
    source = CompileInput("source", 3, content, "hash", 2000)
    row = dict(subject="Project", predicate="OWNER", value="Alice", text="Project OWNER Alice", kind="fact", **kwargs)
    return OpenAICompatibleCompiler.parse(json.dumps({"claims": [row]}), source)[0]


def test_repeated_quote_requires_explicit_location():
    text = "Project OWNER Alice\nProject OWNER Alice"
    with pytest.raises(InvalidEvidence):
        parse(text, quote="Project OWNER Alice")
    draft = parse(text, evidence=[{"quote": "Project OWNER Alice", "start": 20, "end": 39}])
    assert draft.refs == (SourceRef("source", 3, 20, 39, "hash"),)
    assert draft.status == "proposed"


@pytest.mark.parametrize("repair_valid", [True, False])
def test_compiler_schema_feedback_is_bounded_and_never_invents_time(repair_valid):
    content = "Alice must finish the review by 2025-01-01."
    row = dict(
        subject="Alice",
        predicate="TASK",
        value="review",
        text=content,
        kind="fact",
        evidence=[{"span_id": "span-0000"}],
        valid_from=None,
        valid_to="2025-01-01",
    )
    invalid = json.dumps({"claims": [row]})
    corrected = json.dumps({"claims": [{**row, "valid_to": None}]})
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return corrected if len(calls) == 2 and repair_valid else invalid

    compiler = OpenAICompatibleCompiler(base_url="https://example.invalid/v1", model="test", completion=complete)
    source = CompileInput("s", 1, content, "hash", 3000)
    if repair_valid:
        drafts = compiler.compile(source)
        assert drafts[0].valid_from is None and drafts[0].valid_to is None
        assert drafts[0].status == "proposed"
        assert drafts[0].refs[0].content_hash == "hash"
    else:
        with pytest.raises(InvalidEvidence):
            compiler.compile(source)
    assert len(calls) == 2
    assert calls[1][-2] == {"role": "assistant", "content": invalid}
    assert "timezone" in calls[1][-1]["content"]
    assert "Do not invent" in calls[1][-1]["content"]


def test_corrected_response_discards_only_incomplete_candidate_rows():
    content = "Alice owns the review task. Bob owns the deployment task."
    valid = dict(
        subject="Alice", predicate="TASK", value="review", text="Alice owns the review task.", kind="fact",
        evidence=[{"span_id": "span-0000"}], valid_from=None, valid_to=None,
    )
    incomplete = dict(
        subject="Bob", predicate="TASK", value=None, text="Bob owns the deployment task.", kind="fact",
        evidence=[{"span_id": "span-0000"}], valid_from=None, valid_to=None,
    )
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return json.dumps({"claims": [incomplete] if len(calls) == 1 else [valid, incomplete]})

    compiler = OpenAICompatibleCompiler(base_url="https://example.invalid/v1", model="test", completion=complete)
    drafts = compiler.compile(CompileInput("s", 1, content, "hash", 3000))
    assert [(draft.subject, draft.value) for draft in drafts] == [("Alice", "review")]
    assert len(calls) == 2


def test_unicode_offsets_are_characters_not_bytes():
    draft = parse("负责人：张三。", evidence=[{"quote": "张三", "start": 4, "end": 6}])
    assert draft.refs[0].start == 4


def test_schema_correction_does_not_retry_transport_failure():
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        raise TimeoutError("transport timeout")

    compiler = OpenAICompatibleCompiler(base_url="https://example.invalid/v1", model="test", completion=complete)
    with pytest.raises(TimeoutError):
        compiler.compile(CompileInput("s", 1, "Source content", "hash", 3000))
    assert len(calls) == 1


def test_schema_correction_respects_total_input_cap():
    calls = []

    def complete(messages, **kwargs):
        calls.append(messages)
        return "invalid JSON " * 1000

    compiler = OpenAICompatibleCompiler(
        base_url="https://example.invalid/v1", model="test", completion=complete, max_input_chars=2000
    )
    with pytest.raises(InvalidEvidence, match="schema correction exceeds"):
        compiler.compile(CompileInput("s", 1, "Source content", "hash", 3000))
    assert len(calls) == 1


def test_source_handles_bind_literal_offsets_without_generated_arithmetic():
    content = "前言。" * 300 + "Project OWNER Alice" + "尾注。" * 300
    draft = parse(content, evidence=[{"span_id": "span-0001"}])
    ref = draft.refs[0]
    assert ref.start == 800 and ref.end == 1800
    assert content[ref.start : ref.end] == content[800:1800]
    assert draft.status == "proposed"


@pytest.mark.parametrize(
    "span",
    [
        {"span_id": "foreign-source"},
        {"span_id": "span-9999"},
        {"span_id": "span-0000", "quote": "replacement"},
        {"span_id": 0},
    ],
)
def test_unknown_or_mixed_handles_fail_closed(span):
    with pytest.raises(InvalidEvidence):
        parse("Project OWNER Alice", evidence=[span])


def test_multiple_supporting_spans_and_proposed_valid_time():
    content = "Alice joined Project. Ownership began in January."
    draft = parse(
        content,
        evidence=[{"quote": "Alice joined Project."}, {"quote": "Ownership began in January."}],
        valid_from="2025-01-01T00:00:00Z",
        valid_to="2025-02-01T00:00:00Z",
    )
    assert len(draft.refs) == 2 and draft.valid_from.startswith("2025-01-01")
    assert draft.status == "proposed" and draft.metadata["temporal_support"] == "unverified"
    assert not ExactQuoteVerifier().supports(draft, [content[r.start : r.end] for r in draft.refs])


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [{"quote": "abc", "start": True, "end": 3}],
        [{"quote": "abc", "start": 0}],
        [{"quote": "bad", "start": 0, "end": 3}],
        [{"quote": "abc", "start": -1, "end": 2}],
        [{"quote": "abc"}, {"quote": "abc"}],
        [{"quote": "abc"}] * 9,
    ],
)
def test_invalid_span_sets_fail_as_a_whole(evidence):
    with pytest.raises(InvalidEvidence):
        parse("abc", evidence=evidence)


@pytest.mark.parametrize(
    "times",
    [
        {"valid_from": "2025-01-01"},
        {"valid_from": True},
        {"valid_to": "2025-01-01T00:00:00Z"},
        {"valid_from": "2025-02-01T00:00:00Z", "valid_to": "2025-01-01T00:00:00Z"},
    ],
)
def test_invalid_or_implicit_time_bounds_rejected(times):
    with pytest.raises(InvalidEvidence):
        parse("abc", quote="abc", **times)


@pytest.mark.parametrize(
    "text",
    [
        "Project does not have OWNER Alice",
        "Project OWNER Alice is only a proposal",
        "Alice denies being the OWNER of Project",
    ],
)
def test_word_presence_cannot_certify_a_relation(text):
    draft = ClaimDraft(
        subject="Project", predicate="OWNER", value="Alice", text=text, refs=(SourceRef("s", 1, 0, len(text), "hash"),)
    )
    assert not ExactQuoteVerifier().supports(draft, [text])


def test_literal_canonical_verification_cannot_certify_time():
    text = "Project OWNER Alice"
    draft = ClaimDraft(
        subject="Project", predicate="OWNER", value="Alice", text=text, refs=(SourceRef("s", 1, 0, len(text), "hash"),)
    )
    assert ExactQuoteVerifier().supports(draft, [text])
    assert not ExactQuoteVerifier().supports(replace(draft, valid_from="2025-01-01T00:00:00Z"), [text])


def test_worker_keeps_unverified_temporal_candidate_out_of_authoritative_views():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    actor = AuthContext("t", "owner", True)
    memory.create_project(actor, "p", "project")
    memory.ingest(actor, "p", "Project OWNER Alice", request_key="source")

    class Compiler:
        version = "schema-v2-test"

        def compile(self, source):
            return OpenAICompatibleCompiler.parse(
                json.dumps(
                    {
                        "claims": [
                            dict(
                                subject="Project",
                                predicate="OWNER",
                                value="Alice",
                                text=source.content,
                                quote=source.content,
                                kind="fact",
                                valid_from="2025-01-01T00:00:00Z",
                            )
                        ]
                    }
                ),
                source,
            )

    Worker(memory, Compiler(), verifier=ExactQuoteVerifier()).drain()
    row = store.one("SELECT status,valid_from FROM assertion_versions", ())
    assert row["status"] == "proposed" and row["valid_from"].startswith("2025-01-01")
    assert memory.list_claims(actor, "p") == []
    assert memory.read_wiki(actor, "p")["blocks"] == []
    store.close()
