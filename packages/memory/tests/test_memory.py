import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from memory import (
    AccessDenied,
    AuthContext,
    ClaimDraft,
    Conflict,
    ExtractiveCompiler,
    InvalidEvidence,
    MemoryService,
    IngestRoute,
    SourceRef,
    Store,
    Worker,
)
from memory.compiler import OpenAICompatibleCompiler
from memory.models import CompileInput, timestamp


class Clock:
    def __init__(self):
        self.time = datetime(2026, 1, 10, tzinfo=timezone.utc)

    def __call__(self):
        return timestamp(self.time)

    def advance(self, seconds=1):
        self.time += timedelta(seconds=seconds)


@pytest.fixture
def setup(tmp_path):
    store = Store.sqlite(str(tmp_path / "memory.db"))
    store.migrate()
    clock = Clock()
    memory = MemoryService(store, clock=clock)
    owner = AuthContext("t1", "alice", True)
    memory.create_project(owner, "p1", "Project One")
    yield memory, owner, clock
    store.close()


def save(memory, owner, text="Orion OWNER Alice", *, readers=(), key=None, **kwargs):
    receipt = memory.ingest(owner, "p1", text, request_key=key or text, readers=readers, **kwargs)
    return receipt, SourceRef(receipt.source_id, receipt.revision, 0, len(text), receipt.content_hash)


def fact(ref, **kwargs):
    defaults = dict(
        subject="Orion", predicate="OWNER", value="Alice", text="Orion OWNER Alice", refs=(ref,), status="supported"
    )
    defaults.update(kwargs)
    return ClaimDraft(**defaults)


def test_ingest_idempotency_and_immediate_raw_recall(setup):
    memory, owner, _ = setup
    first, _ = save(memory, owner)
    second, _ = save(memory, owner)
    assert first == second
    assert memory.db.one("SELECT COUNT(*) AS n FROM sources")["n"] == 1
    assert memory.db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1
    assert "Alice" in memory.recall(owner, "p1", "Orion owner").context
    with pytest.raises(Conflict):
        save(memory, owner, "different", key="Orion OWNER Alice")


def test_layered_routing_keeps_messages_raw_and_compiles_only_warm_or_project_sources(setup):
    memory, owner, _ = setup
    hot, _ = save(memory, owner, "A short chat message", key="hot", route=IngestRoute())
    warm, _ = save(memory, owner, "A durable decision note", key="warm", route=IngestRoute(durable=True))
    project, _ = save(
        memory, owner, "Orion launch date changed", key="project",
        route=IngestRoute(project_event_kind="milestone"),
    )
    cold, _ = save(memory, owner, "Archived meeting transcript", key="cold", route=IngestRoute(source_kind="transcript"))

    assert (hot.state, hot.job_id) == ("hot", None)
    assert (warm.state, bool(warm.job_id)) == ("warm", True)
    assert (project.state, bool(project.job_id)) == ("project", True)
    assert (cold.state, cold.job_id) == ("cold", None)
    routes = memory.db.all("SELECT layer,route FROM source_routes ORDER BY source_id")
    assert sorted(row["layer"] for row in routes) == ["cold", "hot", "project", "warm"]
    project_route = next(json.loads(row["route"]) for row in routes if row["layer"] == "project")
    assert project_route["project_graph_eligible"] is True
    assert project_route["requires_graph_extraction"] is False
    assert memory.db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='compile'")["n"] == 2


def test_source_and_job_are_atomic(setup):
    memory, owner, _ = setup
    original = memory._job

    def fail(*args, **kwargs):
        raise RuntimeError("simulate outbox failure")

    memory._job = fail
    with pytest.raises(RuntimeError):
        save(memory, owner)
    memory._job = original
    assert memory.db.one("SELECT COUNT(*) AS n FROM sources")["n"] == 0
    assert memory.db.one("SELECT COUNT(*) AS n FROM chunks")["n"] == 0


def test_project_and_tenant_isolation(setup):
    memory, owner, _ = setup
    save(memory, owner)
    for actor in (AuthContext("t2", "alice", True), AuthContext("t1", "stranger", True)):
        with pytest.raises(AccessDenied):
            memory.recall(actor, "p1", "Orion")
    with pytest.raises(AccessDenied):
        memory.create_project(AuthContext("t1", "reader"), "p2", "Two")


def test_source_acl_and_revocation_are_live(setup):
    memory, owner, _ = setup
    bob = AuthContext("t1", "bob")
    memory.set_member(owner, "p1", "bob", "reader")
    receipt, ref = save(memory, owner, readers=("alice", "bob"))
    memory.commit(owner, "p1", [fact(ref)])
    assert memory.read_wiki(bob, "p1")["blocks"]
    memory.set_source_readers(owner, "p1", receipt.source_id, ("alice",))
    assert not memory.recall(bob, "p1", "Orion").items
    assert not memory.read_wiki(bob, "p1")["blocks"]
    assert not memory.traverse(bob, "p1", "Orion")
    with pytest.raises(AccessDenied):
        memory.read_source(bob, "p1", receipt.source_id)


def test_reader_cannot_write_or_escalate(setup):
    memory, owner, _ = setup
    reader = AuthContext("t1", "bob")
    memory.set_member(owner, "p1", "bob", "reader")
    with pytest.raises(AccessDenied):
        save(memory, reader)
    with pytest.raises(AccessDenied):
        memory.set_member(reader, "p1", "bob", "owner")


@pytest.mark.parametrize("bad", ["hash", "span", "scope"])
def test_invalid_evidence_rolls_back_changeset(setup, bad):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    invalid = (
        replace(ref, content_hash="bad")
        if bad == "hash"
        else replace(ref, end=1000)
        if bad == "span"
        else replace(ref, source_id="foreign")
    )
    with pytest.raises((InvalidEvidence, AccessDenied)):
        memory.commit(owner, "p1", [fact(ref), fact(invalid, subject="Other")])
    assert memory.db.one("SELECT COUNT(*) AS n FROM assertions")["n"] == 0
    assert not memory.read_wiki(owner, "p1")["blocks"]


def test_unverified_model_claims_do_not_publish(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    result = memory.commit(owner, "p1", [fact(ref)], compiler_version="model-v1")
    assert result[0]["status"] == "proposed"
    assert not memory.list_claims(owner, "p1")
    assert not memory.read_wiki(owner, "p1")["blocks"]
    assert len(memory.list_claims(owner, "p1", include_candidates=True)) == 1


def test_new_conflicting_single_slot_requires_review(setup):
    memory, owner, _ = setup
    _, alice = save(memory, owner)
    _, bob = save(memory, owner, "Orion OWNER Bob")
    memory.commit(owner, "p1", [fact(alice)])
    result = memory.commit(owner, "p1", [fact(bob, value="Bob", text="Orion OWNER Bob")])
    assert result[0]["status"] == "disputed"
    assert [c["value"] for c in memory.list_claims(owner, "p1")] == ["Alice"]


def test_multivalued_dependency_edges_are_allowed(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner, "Orion depends on Audit and Billing")
    result = memory.commit(
        owner,
        "p1",
        [fact(ref, predicate="DEPENDS_ON", value="Audit"), fact(ref, predicate="DEPENDS_ON", value="Billing")],
    )
    assert all(row["status"] == "supported" for row in result)
    assert len(memory.traverse(owner, "p1", "Orion")) == 2


def test_bitemporal_versions_and_no_future_wiki(setup):
    memory, owner, clock = setup
    _, ref = save(memory, owner)
    first = memory.commit(owner, "p1", [fact(ref, valid_from="2026-01-01T00:00:00Z")])[0]
    before = clock()
    clock.advance(3600)
    _, next_ref = save(memory, owner, "Orion OWNER Bob")
    memory.commit(
        owner,
        "p1",
        [
            fact(
                next_ref,
                assertion_id=first["id"],
                expected_revision=1,
                value="Bob",
                text="Orion OWNER Bob",
                valid_from="2026-01-05T00:00:00Z",
            )
        ],
    )
    assert memory.list_claims(owner, "p1")[0]["value"] == "Bob"
    assert memory.list_claims(owner, "p1", known_at=before)[0]["value"] == "Alice"
    assert memory.list_claims(owner, "p1", valid_at="2026-01-03T00:00:00Z")[0]["value"] == "Alice"
    assert memory.list_claims(owner, "p1", valid_at="2026-01-08T00:00:00Z")[0]["value"] == "Bob"
    old = memory.recall(owner, "p1", "Orion", known_at=before)
    assert "Bob" not in old.context
    assert not any(item.kind == "wiki" for item in old.items)
    assert "Alice" not in memory.recall(owner, "p1", "Orion").context


def test_expired_revision_does_not_resurrect_previous(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    first = memory.commit(owner, "p1", [fact(ref, valid_from="2026-01-01T00:00:00Z")])[0]
    memory.commit(
        owner,
        "p1",
        [
            fact(
                ref,
                assertion_id=first["id"],
                expected_revision=1,
                valid_from="2026-01-02T00:00:00Z",
                valid_to="2026-01-04T00:00:00Z",
            )
        ],
    )
    assert not memory.list_claims(owner, "p1")


def test_human_wiki_protection_and_revision_cas(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    first = memory.commit(owner, "p1", [fact(ref)])[0]
    block = f"assertion:{first['id']}"
    memory.edit_wiki(
        owner, "p1", block, page="fact", text="Human wording: Orion OWNER Alice", refs=(ref,), expected_revision=1
    )
    with pytest.raises(Conflict):
        memory.edit_wiki(owner, "p1", block, page="fact", text="outdated edit", refs=(ref,), expected_revision=1)
    memory.commit(owner, "p1", [fact(ref, assertion_id=first["id"], expected_revision=1)])
    row = memory.db.one("SELECT * FROM wiki_blocks WHERE id=?", (block,))
    assert row["text"] == "Human wording: Orion OWNER Alice"
    assert row["state"] == "stale"
    assert not memory.read_wiki(owner, "p1")["blocks"]


def test_source_revision_invalidates_and_can_be_repaired(setup):
    memory, owner, clock = setup
    receipt, ref = save(memory, owner)
    first = memory.commit(owner, "p1", [fact(ref)])[0]
    clock.advance()
    _, updated = save(memory, owner, "Orion OWNER Bob", source_id=receipt.source_id, expected_revision=1)
    assert not memory.list_claims(owner, "p1")
    assert not memory.read_wiki(owner, "p1")["blocks"]
    memory.commit(
        owner, "p1", [fact(updated, assertion_id=first["id"], expected_revision=1, value="Bob", text="Orion OWNER Bob")]
    )
    assert memory.list_claims(owner, "p1")[0]["value"] == "Bob"
    assert memory.read_wiki(owner, "p1")["blocks"][0]["text"] == "Orion OWNER Bob"


def test_revocation_filters_wiki_graph_raw_and_working_set(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    claim = memory.commit(owner, "p1", [fact(ref)])[0]
    worker = Worker(memory, ExtractiveCompiler())
    worker.drain()
    memory.remember_working_set(owner, "p1", "session", [claim["id"]])
    assert memory.read_working_set(owner, "p1", "session")
    memory.revoke(owner, "p1", claim["id"])
    # Test before the projection worker runs: visibility cannot wait for it.
    assert not memory.list_claims(owner, "p1")
    assert not memory.read_wiki(owner, "p1")["blocks"]
    assert not memory.traverse(owner, "p1", "Orion")
    assert not memory.read_working_set(owner, "p1", "session")
    assert not memory.recall(owner, "p1", "Orion").items
    worker.drain()
    edge = memory.db.one("SELECT * FROM graph_edges WHERE assertion_id=?", (claim["id"],))
    assert edge["active"] == 0


def test_delete_source_purges_content_and_history(setup):
    memory, owner, _ = setup
    receipt, ref = save(memory, owner, "Orion secret is zebra-unicorn")
    memory.commit(owner, "p1", [fact(ref, text="Orion secret is zebra-unicorn", value="zebra-unicorn")])
    Worker(memory, ExtractiveCompiler()).drain()
    memory.delete_source(owner, "p1", receipt.source_id)
    assert not memory.recall(owner, "p1", "zebra-unicorn").items
    for table in (
        "source_versions",
        "assertion_versions",
        "wiki_blocks",
        "wiki_revisions",
        "graph_edges",
        "jobs",
        "changesets",
        "receipts",
    ):
        assert "zebra-unicorn" not in json.dumps(memory.db.all(f"SELECT * FROM {table}"))
    with pytest.raises(AccessDenied):
        memory.read_source(owner, "p1", receipt.source_id, known_at="2026-01-10T00:00:00Z")


def test_raw_recall_budget_is_bounded_and_read_only(setup):
    memory, owner, _ = setup
    save(memory, owner, "Orion important detail. " * 150)
    before = memory.db.one("SELECT sequence FROM projects")["sequence"]
    for budget in (1, 100, 500, 2000):
        result = memory.recall(owner, "p1", "Orion", token_budget=budget)
        assert result.estimated_tokens <= budget
    assert memory.db.one("SELECT sequence FROM projects")["sequence"] == before


def test_unauthorized_vector_hit_is_filtered(setup):
    memory, owner, _ = setup
    memory.set_member(owner, "p1", "bob", "reader")
    save(memory, owner, readers=("alice",))
    cid = memory.db.one("SELECT id FROM chunks")["id"]

    class Index:
        def search(self, *args, **kwargs):
            return [(cid, 1.0), ("nonexisting", 1.0)]

    result = memory.recall(AuthContext("t1", "bob"), "p1", "Orion", vector_index=Index(), query_vector=[1])
    assert not result.items


def test_graph_index_cannot_bypass_live_eligibility(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    claim = memory.commit(owner, "p1", [fact(ref)])[0]

    class Index:
        def outgoing_ids(self, *args, **kwargs):
            return [claim["id"]]

    memory.revoke(owner, "p1", claim["id"])
    assert not memory.traverse(owner, "p1", "Orion", graph_index=Index())


def test_working_set_ttl_and_membership_revoke(setup):
    memory, owner, clock = setup
    _, ref = save(memory, owner)
    cid = memory.commit(owner, "p1", [fact(ref)])[0]["id"]
    memory.set_member(owner, "p1", "bob", "reader")
    bob = AuthContext("t1", "bob")
    memory.remember_working_set(bob, "p1", "s", [cid], ttl_seconds=1)
    clock.advance(2)
    assert not memory.read_working_set(bob, "p1", "s")
    memory.set_member(owner, "p1", "bob", None)
    with pytest.raises(AccessDenied):
        memory.read_working_set(bob, "p1", "s")


def test_worker_durable_restart_and_idempotency(setup, tmp_path):
    memory, owner, _ = setup
    save(memory, owner, "Orion OWNER Alice according to the project meeting.")
    other = Store.sqlite(str(tmp_path / "memory.db"))
    worker = Worker(MemoryService(other), ExtractiveCompiler())
    worker.drain()
    count = other.one("SELECT COUNT(*) AS n FROM assertions")["n"]
    worker.drain()
    assert count == other.one("SELECT COUNT(*) AS n FROM assertions")["n"] == 1
    assert other.one("SELECT state FROM jobs WHERE kind='compile'")["state"] == "committed"
    other.close()


def test_worker_lease_fencing_and_reclaim(setup):
    memory, owner, clock = setup
    save(memory, owner, "Orion OWNER Alice according to the project meeting.")
    worker = Worker(memory, ExtractiveCompiler(), lease_seconds=1)
    old = worker.claim()
    clock.advance(2)
    new = worker.claim()
    assert old["id"] == new["id"] and old["lease_token"] != new["lease_token"]
    worker.process(old)
    assert not memory.list_claims(owner, "p1", include_candidates=True)
    worker.process(new)
    assert len(memory.list_claims(owner, "p1", include_candidates=True)) == 1


def test_worker_cancel_during_compilation(setup):
    memory, owner, _ = setup
    save(memory, owner)
    worker = Worker(memory, ExtractiveCompiler())
    job = worker.claim()
    worker.cancel(owner, "p1", job["id"])
    worker.process(job)
    assert memory.db.one("SELECT state FROM jobs")["state"] == "cancelled"
    assert not memory.list_claims(owner, "p1", include_candidates=True)


def test_worker_source_changed_during_llm_call(setup):
    memory, owner, _ = setup
    receipt, _ = save(memory, owner)

    class Compiler:
        version = "test"

        def compile(self, source):
            save(memory, owner, "Orion OWNER Bob", source_id=receipt.source_id, expected_revision=1)
            return ExtractiveCompiler().compile(source)

    worker = Worker(memory, Compiler())
    worker.run_once()
    assert memory.db.one("SELECT state FROM jobs WHERE id=?", (receipt.job_id,))["state"] == "quarantined"
    assert not memory.list_claims(owner, "p1", include_candidates=True)


def test_worker_failure_retries_are_bounded_and_redacted(setup):
    memory, owner, _ = setup
    save(memory, owner)

    class Broken:
        version = "broken"

        def compile(self, source):
            raise RuntimeError("private provider response SECRET")

    worker = Worker(memory, Broken(), max_attempts=2)
    assert worker.drain() == 2
    row = memory.db.one("SELECT * FROM jobs")
    assert row["state"] == "failed" and row["attempts"] == 2
    assert "SECRET" not in json.dumps(row)


def test_compiler_malformed_output_never_becomes_empty_success():
    source = CompileInput("s", 1, "Orion OWNER Alice", "hash", 1000)
    for raw in ("nonsense", "{}", '{"claims":{}}', '{"claims":[{"quote":"invented"}]}'):
        with pytest.raises(InvalidEvidence):
            OpenAICompatibleCompiler.parse(raw, source)


def test_compiler_exact_quote_is_candidate_not_approval():
    source = CompileInput("s", 1, "Orion OWNER Alice", "hash", 1000)
    row = dict(
        subject="Orion", predicate="OWNER", value="Alice", text=source.content, quote=source.content, kind="fact"
    )
    draft = OpenAICompatibleCompiler.parse(json.dumps({"claims": [row]}), source)[0]
    assert draft.status == "proposed"
    assert draft.refs[0].start == 0 and draft.refs[0].end == len(source.content)


def test_procedure_requires_validation_and_never_executes(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner, "Release procedure: run tests, then request approval.")
    with pytest.raises(InvalidEvidence):
        memory.commit(owner, "p1", [fact(ref, kind="procedure")])
    claim = memory.commit(
        owner,
        "p1",
        [
            fact(
                ref,
                kind="procedure",
                metadata={
                    "preconditions": ["staging only"],
                    "steps": ["run tests", "request approval"],
                    "validation": "reviewed by Alice",
                },
            )
        ],
    )[0]
    assert claim["status"] == "supported"


def test_cross_project_evidence_is_rejected(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner)
    memory.create_project(owner, "p2", "Other")
    with pytest.raises(AccessDenied):
        memory.commit(owner, "p2", [fact(ref)])


def test_write_amplification_is_per_record_not_whole_project(setup):
    memory, owner, _ = setup
    for index in range(30):
        save(memory, owner, f"Project observation number {index}")
    before = memory.db.connection.total_changes
    save(memory, owner, "One additional observation")
    changes = memory.db.connection.total_changes - before
    # source head, source revision, chunk, route receipt, project sequence, job, receipt
    assert changes == 7


def test_compiler_cannot_impersonate_human_review(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner, "Orion OWNER Alice from the project meeting")

    class Compiler:
        version = "human-reviewed"

        def compile(self, source):
            return [fact(ref)]

    Worker(memory, Compiler()).drain()
    assert not memory.list_claims(owner, "p1")
    assert memory.list_claims(owner, "p1", include_candidates=True)[0]["status"] == "proposed"


def test_permission_removed_during_compile_is_rechecked(setup):
    memory, owner, _ = setup
    memory.set_member(owner, "p1", "bob", "editor")
    bob = AuthContext("t1", "bob")
    receipt, ref = save(memory, bob, "Orion OWNER Bob from the project meeting")

    class Compiler:
        version = "test"

        def compile(self, source):
            memory.set_member(owner, "p1", "bob", None)
            return [fact(ref)]

    Worker(memory, Compiler()).run_once()
    assert memory.db.one("SELECT state FROM jobs WHERE id=?", (receipt.job_id,))["state"] == "quarantined"
    assert memory.db.one("SELECT COUNT(*) AS n FROM assertions")["n"] == 0


def test_source_deleted_during_compile_cannot_publish(setup):
    memory, owner, _ = setup
    receipt, ref = save(memory, owner, "Orion OWNER Alice from the project meeting")

    class Compiler:
        version = "test"

        def compile(self, source):
            memory.delete_source(owner, "p1", receipt.source_id)
            return [fact(ref)]

    Worker(memory, Compiler()).run_once()
    assert memory.job_status(owner, "p1", receipt.job_id)["state"] == "quarantined"
    assert not memory.recall(owner, "p1", "Orion").items


def test_bounded_graph_traversal_handles_cycles(setup):
    memory, owner, _ = setup
    _, ref = save(memory, owner, "A depends on B. B depends on A. B depends on C.")
    memory.commit(
        owner,
        "p1",
        [fact(ref, subject=a, predicate="DEPENDS_ON", value=b) for a, b in (("A", "B"), ("B", "A"), ("B", "C"))],
    )
    assert len(memory.traverse(owner, "p1", "A", max_edges=1)) == 1
    assert all(len(path) <= 1 for path in memory.traverse(owner, "p1", "A", max_depth=1))
    assert not memory.traverse(owner, "p1", "A", predicates=("OWNER",))


def test_public_job_status_is_owner_scoped(setup):
    memory, owner, _ = setup
    receipt, _ = save(memory, owner)
    memory.set_member(owner, "p1", "bob", "reader")
    with pytest.raises(AccessDenied):
        memory.job_status(AuthContext("t1", "bob"), "p1", receipt.job_id)
    assert memory.job_status(owner, "p1", receipt.job_id)["state"] == "queued"


def test_historical_claim_can_reference_older_source_revision(setup):
    memory, owner, clock = setup
    receipt, ref = save(memory, owner)
    first = memory.commit(owner, "p1", [fact(ref, valid_from="2026-01-01T00:00:00Z")])[0]
    clock.advance(100)
    _, newer = save(memory, owner, "Orion OWNER Bob", source_id=receipt.source_id, expected_revision=1)
    memory.commit(
        owner,
        "p1",
        [
            fact(
                newer,
                assertion_id=first["id"],
                expected_revision=1,
                value="Bob",
                text="Orion OWNER Bob",
                valid_from="2026-01-05T00:00:00Z",
            )
        ],
    )
    assert memory.list_claims(owner, "p1", valid_at="2026-01-03T00:00:00Z")[0]["value"] == "Alice"
    memory.delete_source(owner, "p1", receipt.source_id)
    assert not memory.list_claims(owner, "p1", valid_at="2026-01-03T00:00:00Z")


def test_independent_exact_verifier_can_publish_extractively_supported_candidate(setup):
    from memory import ExactQuoteVerifier

    memory, owner, _ = setup
    text = "Orion OWNER Alice"
    _, ref = save(memory, owner, text)

    class Compiler:
        version = "candidate-only"

        def compile(self, source):
            return [fact(ref, text=text, status="proposed")]

    Worker(memory, Compiler(), verifier=ExactQuoteVerifier()).drain()
    assert memory.list_claims(owner, "p1")[0]["status"] == "supported"
    assert memory.read_wiki(owner, "p1")["blocks"]
