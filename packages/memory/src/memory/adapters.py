"""Optional real database adapters. Initialization is always explicit."""

from __future__ import annotations

import math
from dataclasses import asdict

from .models import GraphDelta


class Neo4jGraph:
    def __init__(self, driver, *, database="neo4j"):
        self.driver = driver
        self.database = database

    def migrate(self):
        with self.driver.session(database=self.database) as session:
            for label in ("FridayAssertion", "FridayEntity", "FridayValue"):
                session.run(
                    f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE (n.tenant,n.project,n.id) IS UNIQUE"
                ).consume()

    def apply(self, delta: GraphDelta):
        # A real write lock is acquired before the version comparison. Late replay
        # cannot revive a newer tombstone. Entity/value nodes contain no vectors.
        query = """
        MERGE (a:FridayAssertion {tenant:$tenant, project:$project, id:$assertion_id})
        ON CREATE SET a.version=-1, a.lock_counter=0
        SET a.lock_counter=coalesce(a.lock_counter,0)+1
        WITH a WHERE a.version < $version
        OPTIONAL MATCH (a)-[old:SUBJECT|OBJECT]->(previous)
        WITH a, collect(old) AS old_edges, collect(previous) AS old_nodes
        FOREACH (edge IN old_edges | DELETE edge)
        SET a.version=$version, a.active=$active, a.kind=$kind,
            a.predicate=CASE WHEN $active THEN $predicate ELSE '' END
        FOREACH (_ IN CASE WHEN $active THEN [1] ELSE [] END |
            MERGE (s:FridayEntity {tenant:$tenant,project:$project,id:$subject})
            MERGE (o:FridayValue {tenant:$tenant,project:$project,id:$value})
            MERGE (a)-[:SUBJECT]->(s)
            MERGE (a)-[:OBJECT]->(o))
        WITH old_nodes
        UNWIND old_nodes AS n
        WITH DISTINCT n WHERE NOT EXISTS { MATCH (n)--() }
        DELETE n
        """
        with self.driver.session(database=self.database) as session:
            session.execute_write(lambda tx: tx.run(query, **asdict(delta)).consume())

    def outgoing_ids(self, tenant: str, project: str, subject: str, *, limit=200):
        if not 1 <= limit <= 200:
            raise ValueError("graph candidate limit 1..200")
        with self.driver.session(database=self.database) as session:
            result = session.run(
                """MATCH (a:FridayAssertion {tenant:$tenant,project:$project,active:true})
                -[:SUBJECT]->(s:FridayEntity {tenant:$tenant,project:$project,id:$subject})
                RETURN a.id AS id ORDER BY a.id LIMIT $limit""",
                tenant=tenant,
                project=project,
                subject=subject,
                limit=limit,
            )
            return [r["id"] for r in result]


class PgVectorIndex:
    """Incremental chunk index. Search results are hints, never ACL decisions.

    One instance/table schema has a fixed dimension and embedding space. Tenant
    and project predicates always apply. Public recall revalidates every hit.
    """

    def __init__(self, store, *, dimensions: int, model: str):
        if store.dialect != "postgres" or type(dimensions) is not int or not 1 <= dimensions <= 2000 or not model:
            raise ValueError("PostgreSQL, model and dimensions 1..2000 required")
        self.db = store
        self.dimensions = dimensions
        self.model = model

    def migrate(self):
        with self.db.transaction():
            self.db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            self.db.execute(f"""CREATE TABLE IF NOT EXISTS friday_embeddings (
                tenant TEXT NOT NULL,project TEXT NOT NULL,chunk_id TEXT NOT NULL,
                model TEXT NOT NULL,content_hash TEXT NOT NULL,embedding vector({self.dimensions}) NOT NULL,
                PRIMARY KEY(tenant,project,chunk_id,model))""")
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS friday_embeddings_scope ON friday_embeddings(tenant,project,model)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS friday_embeddings_hnsw ON friday_embeddings USING hnsw (embedding vector_cosine_ops)"
            )

    def _vector(self, vector):
        if len(vector) != self.dimensions or not all(math.isfinite(float(v)) for v in vector):
            raise ValueError("embedding dimension or finiteness mismatch")
        if not any(float(v) != 0 for v in vector):
            raise ValueError("zero vector cannot use cosine distance")
        return "[" + ",".join(str(float(v)) for v in vector) + "]"

    def upsert(self, tenant, project, chunk_id, content_hash, vector):
        encoded = self._vector(vector)
        with self.db.transaction():
            # No arbitrary text is stored here; validate scope and content identity.
            chunk = self.db.one(
                "SELECT text FROM chunks WHERE tenant=? AND project=? AND id=?", (tenant, project, chunk_id)
            )
            from .service import digest

            if not chunk or digest(chunk["text"]) != content_hash:
                raise ValueError("chunk missing or changed")
            self.db.execute(
                """INSERT INTO friday_embeddings VALUES(?,?,?,?,?,?::vector)
                ON CONFLICT(tenant,project,chunk_id,model) DO UPDATE SET
                content_hash=excluded.content_hash,embedding=excluded.embedding
                WHERE friday_embeddings.content_hash<>excluded.content_hash""",
                (tenant, project, chunk_id, self.model, content_hash, encoded),
            )

    def search(self, tenant, project, vector, *, limit=50):
        if not 1 <= limit <= 200:
            raise ValueError("vector limit 1..200")
        encoded = self._vector(vector)
        rows = self.db.all(
            """SELECT chunk_id,1-(embedding <=> ?::vector) AS score
            FROM friday_embeddings WHERE tenant=? AND project=? AND model=?
            ORDER BY embedding <=> ?::vector LIMIT ?""",
            (encoded, tenant, project, self.model, encoded, limit),
        )
        return [(r["chunk_id"], max(0.0, float(r["score"]))) for r in rows]

    def contains(self, tenant, project, chunk_id, content_hash):
        return bool(
            self.db.one(
                """SELECT chunk_id FROM friday_embeddings WHERE
            tenant=? AND project=? AND chunk_id=? AND model=? AND content_hash=?""",
                (tenant, project, chunk_id, self.model, content_hash),
            )
        )

    def purge_orphans(self):
        """Physical deletion sweep; serving already ignores nonexisting chunks."""
        with self.db.transaction():
            self.db.execute("""DELETE FROM friday_embeddings e WHERE NOT EXISTS
                (SELECT 1 FROM chunks c WHERE c.tenant=e.tenant AND c.project=e.project AND c.id=e.chunk_id)""")
