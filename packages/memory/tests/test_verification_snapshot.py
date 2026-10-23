from dataclasses import replace
from types import SimpleNamespace

import pytest

from memory import AuthContext, ClaimDraft, MemoryService, SourceRef, Store, Worker
from memory.models import InvalidEvidence
from memory.verification import prepare_verification


def draft():
    return ClaimDraft(
        subject="Project",
        predicate="OWNER",
        value="Alice",
        text="Project OWNER Alice",
        refs=(SourceRef("s", 1, 0, 19, "hash"),),
    )


def test_prepared_result_bound_to_every_candidate_field_and_evidence():
    d = draft()
    prepared = prepare_verification(SimpleNamespace(supports=lambda *a: True), [d], [[d.text]])
    assert prepared.supports(d, [d.text])
    for changed in [
        replace(d, value="Bob"),
        replace(d, valid_from="2025-01-01T00:00:00Z"),
        replace(d, refs=(replace(d.refs[0], revision=2),)),
        replace(d, metadata={"altered": True}),
    ]:
        assert not prepared.supports(changed, [d.text])
    assert not prepared.supports(d, ["different source text"])


def test_verifier_cannot_mutate_snapshot_or_use_truthy_strings():
    d = draft()

    def mutates(candidate, texts):
        candidate.metadata["injected"] = True
        return True

    for callback in [mutates, lambda *a: "true"]:
        with pytest.raises(InvalidEvidence):
            prepare_verification(SimpleNamespace(supports=callback), [d], [[d.text]])
    assert d.metadata == {}


def test_checkpoint_runs_before_each_external_verification():
    events = []

    def checkpoint():
        events.append("checkpoint")

    def verify(*args):
        events.append("verify")
        return True

    d = draft()
    prepare_verification(SimpleNamespace(supports=verify), [d, d], [[d.text], [d.text]], checkpoint=checkpoint)
    assert events == ["checkpoint", "verify", "checkpoint", "verify"]


@pytest.mark.parametrize("action", ["none", "overwrite", "revoke_member"])
def test_verification_runs_outside_transaction_and_commit_rechecks_state(action):
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    admin = AuthContext("t", "admin", True)
    writer = AuthContext("t", "writer")
    memory.create_project(admin, "p", "project")
    memory.set_member(admin, "p", "writer", "editor")
    receipt = memory.ingest(writer, "p", "Project OWNER Alice", request_key="initial")

    class Compiler:
        version = "test"

        def compile(self, source):
            return [
                replace(
                    draft(),
                    refs=(SourceRef(source.source_id, source.revision, 0, len(source.content), source.content_hash),),
                )
            ]

    observed = []

    class Verifier:
        def supports(self, candidate, texts):
            observed.append(store.connection.in_transaction)
            if action == "overwrite":
                memory.ingest(
                    writer,
                    "p",
                    "Project OWNER Bob",
                    request_key="overwrite",
                    source_id=receipt.source_id,
                    expected_revision=1,
                )
            elif action == "revoke_member":
                memory.set_member(admin, "p", "writer", None)
            return True

    worker = Worker(memory, Compiler(), verifier=Verifier(), max_attempts=1)
    job = worker.claim()
    worker.process(job)
    assert observed == [False]
    count = store.one("SELECT COUNT(*) AS n FROM assertion_versions WHERE status='supported'", ())["n"]
    assert count == (1 if action == "none" else 0)
    store.close()
