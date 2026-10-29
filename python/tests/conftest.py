import pytest


@pytest.fixture(autouse=True)
def gateway_test_key(monkeypatch):
    # All external services are replaced by local transports in these tests.
    monkeypatch.setenv("COSENS_API_KEY", "test-only-gateway-key")
    monkeypatch.setenv("COSENS_REGION", "cn")


@pytest.fixture
def empty_recall(monkeypatch):
    """Simulate a legitimate no-match adapter result for native compatibility tests."""
    from cosens.atoms import AsyncAtoms, SyncAtoms
    from cosens.models import result
    queries = []
    def recall(self, *, query, task_family="general"):
        queries.append(query)
        return result({"recipe": {}, "context": {}, "snapshot": {}})
    async def arecall(self, *, query, task_family="general"):
        return recall(self, query=query)
    monkeypatch.setattr(SyncAtoms, "recall_context", recall)
    monkeypatch.setattr(AsyncAtoms, "recall_context", arecall)
    return queries
