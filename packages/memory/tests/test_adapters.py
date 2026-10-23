import os
from uuid import uuid4

import pytest

from memory.adapters import Neo4jGraph, PgVectorIndex
from memory.models import GraphDelta


def test_neo4j_roundtrip_and_out_of_order_tombstone():
    uri = os.environ.get("MEMORY_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("MEMORY_TEST_NEO4J_URI not set")
    neo4j = pytest.importorskip("neo4j")
    driver = neo4j.GraphDatabase.driver(
        uri, auth=(os.environ["MEMORY_TEST_NEO4J_USER"], os.environ["MEMORY_TEST_NEO4J_PASSWORD"])
    )
    graph = Neo4jGraph(driver, database=os.environ.get("MEMORY_TEST_NEO4J_DATABASE", "neo4j"))
    tenant = "friday-test-" + uuid4().hex
    try:
        graph.migrate()
        delta = GraphDelta(tenant, "p", "a", 1, "Orion", "OWNER", "Alice", "fact", True)
        graph.apply(delta)
        graph.apply(delta)
        assert graph.outgoing_ids(tenant, "p", "Orion") == ["a"]
        graph.apply(GraphDelta(tenant, "p", "a", 2, "", "", "", "fact", False))
        graph.apply(delta)
        assert not graph.outgoing_ids(tenant, "p", "Orion")
        with driver.session(database=graph.database) as session:
            assert session.run("MATCH (n:FridayValue {tenant:$t}) RETURN count(n) AS n", t=tenant).single()["n"] == 0
    finally:
        with driver.session(database=graph.database) as session:
            session.run(
                "MATCH (n {tenant:$t}) WHERE n:FridayAssertion OR n:FridayEntity OR n:FridayValue DETACH DELETE n",
                t=tenant,
            ).consume()
        driver.close()


def test_vector_adapter_validates_dimensions():
    class DB:
        dialect = "postgres"

    index = PgVectorIndex(DB(), dimensions=3, model="test")
    assert index._vector([1, 2, 3]) == "[1.0,2.0,3.0]"
    for vector in ([1, 2], [0, 0, 0], [float("nan"), 0, 1]):
        with pytest.raises(ValueError):
            index._vector(vector)
