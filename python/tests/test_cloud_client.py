"""Cloud mode: RSIClient() talks to the CoSens service with the same atoms."""

import base64
import json

import httpx
import pytest

from cosens.rsi import CloudRSIClient, Conflict, InvalidState, NotFound, RSIClient


def recording(responses=None):
    seen = []

    def handler(request):
        body = json.loads(request.content) if request.content else None
        seen.append({"method": request.method, "path": request.url.raw_path.decode().split("?")[0], "params": dict(request.url.params), "headers": request.headers, "body": body})
        status, payload = (responses or {}).get((request.method, request.url.raw_path.decode().split("?")[0]), (200, {"ok": True}))
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler), seen


def test_default_is_cloud_and_data_dir_selects_local(tmp_path, monkeypatch):
    monkeypatch.setenv("COSENS_API_KEY", "k")
    assert isinstance(RSIClient(), CloudRSIClient)
    local = RSIClient(data_dir=tmp_path, config=None)
    assert local.mode == "local"
    with pytest.raises(ValueError):
        RSIClient(mode="cloud", data_dir=tmp_path)
    monkeypatch.delenv("COSENS_API_KEY")
    with pytest.raises(ValueError, match="COSENS_API_KEY"):
        RSIClient()


def test_create_uploads_only_the_named_files(tmp_path):
    (tmp_path / "context.txt").write_text("[user] weekly report")
    (tmp_path / "sales.csv").write_text("region,amount\n")
    (tmp_path / "chart.png").write_bytes(b"\x89PNG\x00\xff")
    (tmp_path / "RECIPE.md").write_text("Group sales by region.")
    (tmp_path / "unrelated.txt").write_text("stays local")
    transport, seen = recording()
    client = CloudRSIClient(api_key="k", base_url="https://rsi.example/api", transport=transport)
    with pytest.raises(InvalidState, match="requires local mode"):
        client.recipe_insert(tmp_path / "RECIPE.md")
    assert seen == []
    client.execution_create(
        evidence={
            "context_path": str(tmp_path / "context.txt"),
            "files": [
                {"path": str(tmp_path / "sales.csv"), "role": "input"},
                {"path": str(tmp_path / "chart.png"), "role": "artifact"},
            ],
        },
        idempotency_key="delivery-1",
        recipe_reference={"path": str(tmp_path / "RECIPE.md"), "recipe_id": "recipe_1"},
    )
    request = seen[0]
    assert (request["method"], request["path"]) == ("POST", "/api/executions")
    assert request["headers"]["authorization"] == "Bearer k"
    assert request["headers"]["user-agent"].startswith("cosens-sdk-python/")
    body = request["body"]
    assert body["idempotency_key"] == "delivery-1"
    assert body["context"] == {"name": "context.txt", "content": "[user] weekly report", "encoding": "utf-8"}
    assert [f["name"] for f in body["files"]] == ["sales.csv", "chart.png", "RECIPE.md"]
    assert body["files"][1]["encoding"] == "base64"
    assert base64.b64decode(body["files"][1]["content"]) == b"\x89PNG\x00\xff"
    assert body["files"][2] == {
        "name": "RECIPE.md",
        "content": "Group sales by region.",
        "encoding": "utf-8",
        "role": "recipe",
        "recipe_id": "recipe_1",
    }
    assert "recipe_reference" not in body
    assert "stays local" not in json.dumps(body)


def test_reads_and_queries_map_to_routes():
    transport, seen = recording()
    client = CloudRSIClient(api_key="k", base_url="https://rsi.example/api", transport=transport)
    client.execution_get("e/1")
    client.execution_list(query={"execution_ids": ["a", "b"]})
    client.recipe_list()
    client.recipe_get("r1", version="active")
    client.rubrics_list(offset=50, archived=True)
    client.recipe_version_set_archived("r1", version_id="v1", archived=True)
    client.execution_clarification_answer("e1", question_id="q", question_revision=1, answer=["opt"])
    client.recipe_test(execution_id="e1", recipe_id="r1", version_id="v1", rubrics_version_id="b1")
    assert [(s["method"], s["path"]) for s in seen] == [
        ("GET", "/api/executions/e%2F1"),
        ("GET", "/api/executions"),
        ("GET", "/api/recipes"),
        ("GET", "/api/recipes/r1"),
        ("GET", "/api/rubrics"),
        ("PUT", "/api/recipes/r1/versions/v1/archived"),
        ("POST", "/api/executions/e1/clarifications"),
        ("POST", "/api/executions/e1/recipe-tests"),
    ]
    assert seen[1]["params"] == {"execution_ids": "a,b"}
    assert seen[3]["params"] == {"version": "active"}
    assert seen[4]["params"] == {"offset": "50", "archived": "true"}
    assert client.execution_list(query={"execution_ids": []}) == []
    assert client.recover() == {"started": 0}
    assert client.recover(recipe_id="r1") == {"started": 0}
    assert not hasattr(client, "recipe_retry")
    with pytest.raises(InvalidState, match="requires local mode"):
        client.recover(recipe_id="r1", retry_key="retry")


def test_service_errors_keep_their_codes():
    transport, _ = recording(
        {
            ("GET", "/api/executions/missing"): (404, {"error": {"code": "not_found", "message": "not found"}}),
            ("POST", "/api/executions/e1/cancel"): (409, {"error": {"code": "conflict", "message": "done"}}),
            ("GET", "/api/recipes/bad"): (422, {"error": {"code": "invalid_request", "message": "bad version"}}),
        }
    )
    client = CloudRSIClient(api_key="k", base_url="https://rsi.example/api", transport=transport)
    with pytest.raises(NotFound):
        client.execution_get("missing")
    with pytest.raises(Conflict):
        client.execution_cancel("e1")
    with pytest.raises(ValueError, match="bad version"):
        client.recipe_get("bad", version="x")


def test_uploads_over_the_gateway_limit_fail_before_sending(tmp_path):
    (tmp_path / "context.txt").write_text("c")
    (tmp_path / "big.bin").write_bytes(b"\xff" * (8 * 1024 * 1024))
    transport, seen = recording()
    client = CloudRSIClient(api_key="k", base_url="https://rsi.example/api", transport=transport)
    with pytest.raises(ValueError, match="10 MiB"):
        client.execution_create(
            evidence={"context_path": str(tmp_path / "context.txt"), "files": [{"path": str(tmp_path / "big.bin"), "role": "input"}]},
            idempotency_key="k1",
        )
    assert seen == []


def test_virtual_user_is_instance_scoped_and_rejected_in_local_mode(tmp_path):
    transport, seen = recording()
    a = CloudRSIClient(api_key="k", transport=transport, virtual_user_id="vu_a")
    b = CloudRSIClient(api_key="k", transport=transport, virtual_user_id="vu_b")
    ordinary = CloudRSIClient(api_key="k", transport=transport)
    a.execution_get("a")
    b.execution_get("b")
    ordinary.execution_get("c")
    assert [r["headers"].get("x-virtual-user-id") for r in seen] == ["vu_a", "vu_b", None]
    assert isinstance(RSIClient(api_key="k", virtual_user_id="vu_a"), CloudRSIClient)
    with pytest.raises(ValueError):
        RSIClient(data_dir=tmp_path, virtual_user_id="vu_a")
