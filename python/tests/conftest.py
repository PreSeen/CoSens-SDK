import pytest


@pytest.fixture(autouse=True)
def gateway_test_key(monkeypatch):
    # All external services are replaced by local transports in these tests.
    monkeypatch.setenv("COSENS_API_KEY", "test-only-gateway-key")
