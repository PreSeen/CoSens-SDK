import pytest

from memory import AccessDenied, AuthContext, Conflict, ExperienceService, MemoryService, Store


@pytest.fixture
def experience_memory(tmp_path):
    store = Store.sqlite(str(tmp_path / "memory.sqlite"))
    store.migrate()
    store.migrate()
    memory = MemoryService(store)
    actor = AuthContext("tenant", "alice", True)
    memory.create_project(actor, "project", "Experience tests")
    yield ExperienceService(memory), memory, actor
    store.close()


def record(**changes):
    data = {"run_id": "run-a", "task_family": "api", "phase": "assist",
            "action_schema": "actions-v1", "reward_spec": "quality-v1",
            "state": {"observation": "response schema validation failure"},
            "environment": {"python": "3.12"}, "candidates": [
                {"id": "inspect-schema", "description": "Inspect the response schema"}],
            "actual_action_id": "inspect-schema"}
    data.update(changes)
    return data


def search(experiences, actor, **changes):
    options = dict(task_family="api", phase="assist", environment={"python": "3.12"},
                   action_schema="actions-v1", reward_spec="quality-v1",
                   query="response schema validation failure")
    options.update(changes)
    return experiences.search(actor, "project", **options)


def verify(experiences, actor, receipt, *, value=1, key="verify", status="verified"):
    return experiences.append_outcome(actor, "project", receipt["experience_id"],
        {"evaluation_id": key, "status": status, "value": value, "reward_spec": "quality-v1",
         "evidence_ref": "artifact:report"}, request_key=key, expected_revision=receipt["revision"])


def test_typed_records_do_not_leak_into_knowledge_or_compile(experience_memory):
    experiences, memory, actor = experience_memory
    receipt = experiences.upsert(actor, "project", record(outcome={"status": "verified", "value": 1}), request_key="write")
    assert experiences.get(actor, "project", receipt["experience_id"])["record"]["outcome"]["status"] == "pending"
    assert not search(experiences, actor)["items"]
    assert not memory.recall(actor, "project", "response schema validation failure").items
    for table in ("sources", "chunks", "jobs", "wiki_blocks", "assertions"):
        assert memory.db.one(f"SELECT COUNT(*) n FROM {table}")["n"] == 0
    verify(experiences, actor, receipt)
    assert len(search(experiences, actor)["items"]) == 1


def test_rewards_append_versions_and_corrections_obey_watermark(experience_memory):
    experiences, _, actor = experience_memory
    first = experiences.upsert(actor, "project", record(), request_key="write")
    pending_cutoff = experiences.watermark(actor, "project")
    positive = verify(experiences, actor, first)
    positive_cutoff = positive["watermark"]
    assert not search(experiences, actor, watermark=pending_cutoff)["items"]
    assert search(experiences, actor)["items"][0]["record"]["outcome"]["value"] == 1
    assert experiences.get(actor, "project", first["experience_id"], revision=1)["record"]["outcome"]["status"] == "pending"
    negative = verify(experiences, actor, positive, value=-1, key="correction")
    assert negative["revision"] == 3
    # A correction invalidates old scoring even when a caller pins an old cutoff.
    assert not search(experiences, actor, watermark=positive_cutoff)["items"]
    assert search(experiences, actor)["items"][0]["record"]["outcome"]["value"] == -1
    verify(experiences, actor, negative, key="retract", status="retracted")
    assert not search(experiences, actor)["items"]


def test_idempotency_conflicts_and_reward_authority(experience_memory):
    experiences, _, actor = experience_memory
    first = experiences.upsert(actor, "project", record(), request_key="write")
    assert experiences.upsert(actor, "project", record(), request_key="write") == first
    with pytest.raises(Conflict):
        experiences.upsert(actor, "project", record(run_id="changed"), request_key="write")
    with pytest.raises(AccessDenied):
        verify(experiences, AuthContext(actor.tenant, actor.principal), first)
    second = verify(experiences, actor, first)
    assert verify(experiences, actor, first) == second
    with pytest.raises(Conflict):
        verify(experiences, actor, first, value=-1)
    with pytest.raises(Conflict):
        verify(experiences, actor, first, key="stale")


def test_permissions_and_withdrawal_apply_to_historical_reads(experience_memory):
    experiences, memory, actor = experience_memory
    bob = AuthContext("tenant", "bob")
    memory.set_member(actor, "project", "bob", "reader")
    first = experiences.upsert(actor, "project", record(), request_key="private", readers=("alice",))
    verify(experiences, actor, first)
    assert not search(experiences, bob)["items"]
    with pytest.raises(AccessDenied):
        experiences.get(bob, "project", first["experience_id"])
    public = experiences.upsert(actor, "project", record(run_id="public"), request_key="public")
    verify(experiences, actor, public, key="public-verify")
    assert len(search(experiences, bob)["items"]) == 1
    experiences.set_readers(actor, "project", public["experience_id"], ["alice"])
    assert not search(experiences, bob)["items"]
    memory.set_member(actor, "project", "bob", None)
    with pytest.raises(AccessDenied):
        search(experiences, bob)
    experiences.withdraw(actor, "project", public["experience_id"])
    with pytest.raises(AccessDenied):
        experiences.get(actor, "project", public["experience_id"], revision=1)
    assert len(search(experiences, actor)["items"]) == 1


@pytest.mark.parametrize("change", ["revision", "acl", "delete"])
def test_live_source_changes_remove_experiences(experience_memory, change):
    experiences, memory, actor = experience_memory
    text = "response schema validation failure"
    source = memory.ingest(actor, "project", text, request_key="source")
    ref = {"source_id": source.source_id, "revision": 1, "content_hash": source.content_hash,
           "start": 0, "end": len(text)}
    receipt = experiences.upsert(actor, "project", record(source_refs=[ref]), request_key="record")
    verify(experiences, actor, receipt)
    memory.set_member(actor, "project", "bob", "reader")
    bob = AuthContext("tenant", "bob")
    assert search(experiences, bob)["items"]
    if change == "revision":
        memory.ingest(actor, "project", text + " corrected", source_id=source.source_id,
                      expected_revision=1, request_key="revise")
    elif change == "acl":
        memory.set_source_readers(actor, "project", source.source_id, ("alice",))
    else:
        memory.delete_source(actor, "project", source.source_id)
    assert not search(experiences, bob)["items"]


def test_scope_filters_exclude_evaluation_and_incompatible_experience(experience_memory):
    experiences, _, actor = experience_memory
    for i, changes in enumerate(({}, {"partition": "confirm"}, {"environment": {"python": "3.10"}},
                                  {"contract_hash": "incompatible-contract"},
                                  {"task_family": "other"}, {"action_schema": "other"})):
        receipt = experiences.upsert(actor, "project", record(**changes), request_key=f"write-{i}")
        verify(experiences, actor, receipt, key=f"verify-{i}")
    assert len(search(experiences, actor)["items"]) == 1
    assert not search(experiences, actor, exclude_run_id="run-a")["items"]
    with pytest.raises(AccessDenied):
        search(experiences, AuthContext("other-tenant", "alice", True))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 2])
def test_invalid_numeric_rewards_do_not_append(experience_memory, value):
    experiences, _, actor = experience_memory
    receipt = experiences.upsert(actor, "project", record(), request_key="write")
    with pytest.raises(ValueError):
        verify(experiences, actor, receipt, value=value)
    assert experiences.get(actor, "project", receipt["experience_id"])["revision"] == 1


def test_revising_observation_discards_previous_reward(experience_memory):
    experiences, _, actor = experience_memory
    first = experiences.upsert(actor, "project", record(), request_key="write")
    second = verify(experiences, actor, first)
    changed = record(state={"observation": "different schema failure"})
    revised = experiences.upsert(actor, "project", changed, experience_id=first["experience_id"],
                                  expected_revision=second["revision"], request_key="revise")
    assert revised["revision"] == 3
    assert revised["reward_status"] == "pending"
    assert not search(experiences, actor)["items"]


def test_experience_and_existing_knowledge_survive_reopen_and_migrate(tmp_path):
    path = str(tmp_path / "persisted.sqlite")
    actor = AuthContext("tenant", "alice", True)
    store = Store.sqlite(path)
    store.migrate()
    memory = MemoryService(store)
    memory.create_project(actor, "project", "Persistence")
    source = memory.ingest(actor, "project", "A separate project fact", request_key="fact")
    experiences = ExperienceService(memory)
    first = experiences.upsert(actor, "project", record(), request_key="write")
    verify(experiences, actor, first)
    store.close()
    reopened = Store.sqlite(path)
    reopened.migrate()
    memory = MemoryService(reopened)
    assert memory.read_source(actor, "project", source.source_id)["content"] == "A separate project fact"
    assert len(search(ExperienceService(memory), actor)["items"]) == 1
    reopened.close()
