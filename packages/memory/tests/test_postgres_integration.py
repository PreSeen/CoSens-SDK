"""Opt-in tests. MEMORY_TEST_POSTGRES_DSN must target a disposable test database."""

import os
from uuid import uuid4

import pytest

from memory import AuthContext, ClaimDraft, ExtractiveCompiler, MemoryService, SourceRef, Store, Worker


@pytest.fixture
def postgres():
    dsn = os.environ.get("MEMORY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("MEMORY_TEST_POSTGRES_DSN not set")
    pytest.importorskip("psycopg")
    db = Store.postgres(dsn)
    # A generated private schema: never truncate caller tables.
    schema = "friday_test_" + uuid4().hex
    db.execute(f"CREATE SCHEMA {schema}")
    db.execute(f"SET search_path TO {schema},public")
    db.migrate()
    yield db, dsn, schema
    db.execute("SET search_path TO public")
    db.execute(f"DROP SCHEMA {schema} CASCADE")
    db.close()


def test_postgres_end_to_end(postgres):
    db, _, _ = postgres
    memory = MemoryService(db)
    actor = AuthContext("integration", "tester", True)
    memory.create_project(actor, "project", "PG integration")
    text = "Orion OWNER Alice according to the approved project record."
    receipt = memory.ingest(actor, "project", text, request_key="source-1")
    assert receipt == memory.ingest(actor, "project", text, request_key="source-1")
    ref = SourceRef(receipt.source_id, 1, 0, len(text), receipt.content_hash)
    claim = memory.commit(
        actor,
        "project",
        [ClaimDraft(subject="Orion", predicate="OWNER", value="Alice", text=text, refs=(ref,), status="supported")],
    )[0]
    assert memory.recall(actor, "project", "Orion owner").items
    assert memory.read_wiki(actor, "project", level=2)["blocks"]
    Worker(memory, ExtractiveCompiler()).drain()
    assert db.one("SELECT COUNT(*) AS n FROM graph_edges WHERE active=1")["n"] == 1
    memory.revoke(actor, "project", claim["id"])
    assert not memory.traverse(actor, "project", "Orion")
    memory.delete_source(actor, "project", receipt.source_id)
    assert not memory.recall(actor, "project", "Orion").items


def test_postgres_two_workers_do_not_claim_same_job(postgres):
    db, dsn, schema = postgres
    memory = MemoryService(db)
    actor = AuthContext("integration", "tester", True)
    memory.create_project(actor, "project", "Workers")
    for index in range(2):
        memory.ingest(actor, "project", f"Observation number {index} for this project", request_key=str(index))
    second = Store.postgres(dsn)
    second.execute(f"SET search_path TO {schema},public")
    try:
        w1 = Worker(memory, ExtractiveCompiler())
        w2 = Worker(MemoryService(second), ExtractiveCompiler())
        a, b = w1.claim(), w2.claim()
        assert a["id"] != b["id"]
        w1.process(a)
        w2.process(b)
        assert db.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='compile' AND state='committed'")["n"] == 2
    finally:
        second.close()


def test_pgvector_roundtrip_and_purge(postgres):
    from memory.adapters import PgVectorIndex
    from memory.service import digest

    db, _, _ = postgres
    if not db.one("SELECT name FROM pg_available_extensions WHERE name='vector'"):
        pytest.skip("pgvector extension not installed on test server")
    index = PgVectorIndex(db, dimensions=3, model="test-embedding")
    index.migrate()
    memory = MemoryService(db)
    actor = AuthContext("integration", "tester", True)
    memory.create_project(actor, "project", "Vectors")
    receipt = memory.ingest(actor, "project", "Project Orion details", request_key="source")
    chunk = db.one("SELECT * FROM chunks")
    index.upsert(actor.tenant, "project", chunk["id"], digest(chunk["text"]), [1, 0, 0])
    assert index.search(actor.tenant, "project", [1, 0, 0])[0][0] == chunk["id"]
    assert not index.search("foreign", "project", [1, 0, 0])
    assert memory.recall(actor, "project", "semantic lookup", vector_index=index, query_vector=[1, 0, 0]).items
    memory.delete_source(actor, "project", receipt.source_id)
    assert not memory.recall(actor, "project", "lookup", vector_index=index, query_vector=[1, 0, 0]).items
    index.purge_orphans()
    assert not index.search(actor.tenant, "project", [1, 0, 0])


def test_pgvector_worker_with_batch_checkpoint(postgres):
    from memory.adapters import PgVectorIndex

    db, _, _ = postgres
    if not db.one("SELECT name FROM pg_available_extensions WHERE name='vector'"):
        pytest.skip("pgvector extension not installed")
    index = PgVectorIndex(db, dimensions=3, model="test-embedding")
    index.migrate()
    memory = MemoryService(db, enable_embedding_jobs=True)
    actor = AuthContext("integration", "tester", True)
    memory.create_project(actor, "project", "Async vectors")
    memory.ingest(actor, "project", "Orion requires a security audit before rollout.", request_key="source")

    class Embedder:
        model = "test-embedding"
        calls = 0

        def embed(self, texts):
            self.calls += 1
            return [[1, 0, 0] for text in texts]

    embedder = Embedder()
    worker = Worker(memory, ExtractiveCompiler(), vector_index=index, embedder=embedder)
    worker.drain()
    assert db.one("SELECT state FROM jobs WHERE kind='embed'")["state"] == "committed"
    assert embedder.calls == 1
    # Replay an already completed job; stored chunk hashes prevent another paid embedding call.
    with db.transaction():
        db.execute("UPDATE jobs SET state='queued' WHERE kind='embed'")
    worker.drain()
    assert embedder.calls == 1
    assert memory.recall(actor, "project", "gate", query_vector=[1, 0, 0], vector_index=index).items
