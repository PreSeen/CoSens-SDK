import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email import policy
from email.parser import BytesParser
from pathlib import Path
from threading import Barrier

import httpx
import pytest

from cosens.rsi import CloudRSIClient, Conflict, InvalidState, NotFound, RSIClient, cloud
from cosens.rsi.recipe_file import read_recipe_file
from cosens.rsi.recipe_identity import PENDING
from cosens.rsi.recipe_package import build_recipe_package
from cosens.rsi.recipe_sync import upload_recipe

RECIPE = "recipe_official"
VERSION = "version_official"
NEXT_VERSION = "version_official_next"
MARKDOWN = "---\nname: Confirm balances\ndescription: Verify the balance\n---\n\nUse the published baseline.\n"


def _setup(tmp_path, monkeypatch, *, shared_headers=True, rsi_base_url=None):
    context = tmp_path / "context.txt"
    context.write_text("Check the balance.")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "RECIPE.md").write_text(MARKDOWN)
    (payload / "helpers").mkdir()
    (payload / "helpers" / "snippet.txt").write_text("supporting data")
    archive = build_recipe_package(payload)
    monkeypatch.setenv("COSENS_RSI_BASE_URL", "https://rsi.example/api")
    monkeypatch.setenv("COSENS_API_KEY", "fixture-cloud-api-key")
    options = {
        "auto_start": False,
        "rsi_base_url": rsi_base_url,
        **({"headers": {"Authorization": "Bearer fixture-cloud-token", "X-Auth-Type": "access_token"}} if shared_headers else {}),
    }
    client = RSIClient(
        data_dir=tmp_path / "local", model_id="local-model", api_key="fixture-model-key",
        base_url="https://model.example/v1", config=options,
    )
    request = {
        "evidence": {"context_path": str(context), "files": []},
        "idempotency_key": "current-task",
        "recipe_reference": {"recipe_id": RECIPE, "path": str(tmp_path / "caller-recipe-not-present.md")},
    }
    return client, request, archive


def _cloud(monkeypatch, archive, *, selected=VERSION, active=VERSION, published="published", sha256=None,
           authorization="Bearer fixture-cloud-token", metadata_barrier=None):
    calls = []
    signed = f"https://objects.example/{selected}.zip?signature=fixture"

    def send(request):
        calls.append((request.method, request.url.path, request.url.params.get("version")))
        if str(request.url) == signed:
            assert request.method == "GET"
            assert "Authorization" not in request.headers
            return httpx.Response(200, content=archive)
        assert request.headers["Authorization"] == authorization
        assert request.url.host == "rsi.example"
        if request.url.path == f"/api/v1/recipes/{RECIPE}/versions/{selected}/download-url":
            assert request.method == "POST"
            return httpx.Response(200, json={"url": signed})
        if request.method == "POST":
            assert request.url.path == "/api/v1/recipes_upsert"
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + request.content
            )
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in message.iter_parts()
            }
            assert fields["recipe_id"] == RECIPE.encode()
            return httpx.Response(200, json={"recipe_id": RECIPE, "recipe_version_id": NEXT_VERSION})
        if request.url.path == f"/api/v1/recipes/{RECIPE}":
            if metadata_barrier is not None:
                metadata_barrier.wait(timeout=5)
            return httpx.Response(200, json={
                "id": RECIPE,
                "active_version_id": active,
                "selected_version": {
                    "id": selected, "object_id": RECIPE, "publication_state": published,
                    "package_sha256": sha256 or hashlib.sha256(archive).hexdigest(),
                },
            })
        pytest.fail(f"Unexpected Recipe request: {request.method} {request.url.path}")

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **kw: CloudRSIClient(transport=transport, **kw))
    return calls


def test_bootstrap_pins_official_package_and_next_upload_keeps_recipe_id(tmp_path, monkeypatch):
    rsi_base_url = "https://rsi.example/api/v1/"
    client, request, archive = _setup(tmp_path, monkeypatch, rsi_base_url=rsi_base_url)
    monkeypatch.setenv("COSENS_RSI_BASE_URL", "https://ignored.example/api")
    calls = _cloud(monkeypatch, archive)
    created = client.execution_create(**request)
    assert created["recipe_id"] == RECIPE
    assert [(method, selector) for method, _, selector in calls] == [("GET", "active"), ("POST", None), ("GET", None)]
    store = client._queue.store
    saved = store.get(created["id"], "execution")
    assert saved["base_recipe_version_id"] == saved["publication_base_version_id"] == VERSION
    base = client.recipe_get(RECIPE, version=VERSION)
    assert base["active_version_id"] == VERSION
    assert base["selected_version"]["content"]["instructions"] == "Use the published baseline.\n"
    directory = client.data_dir / base["storage_path"] / VERSION
    assert (directory / "helpers" / "snippet.txt").read_text() == "supporting data"
    assert Path(next(file["path"] for file in created["evidence"] if file["role"] == "recipe")).read_text() == MARKDOWN
    assert client.execution_create(**request)["id"] == created["id"]
    with closing(cloud.CloudRSIClient(base_url=rsi_base_url, _shared_headers=client._upload_headers)) as service:
        with pytest.raises(InvalidState, match="already published"):
            upload_recipe(store, service, "already-published.zip", archive, RECIPE, VERSION, None)

    with monkeypatch.context() as missing_endpoint:
        missing_endpoint.delenv("COSENS_RSI_BASE_URL")
        offline_client = RSIClient(
            data_dir=client.data_dir, model_id="local-model", api_key="fixture-model-key",
            base_url="https://model.example/v1", config={"auto_start": False},
        )
        offline = offline_client.execution_create(**{**request, "idempotency_key": "another-task"})
        offline_client.execution_cancel(offline["id"])
    assert store.get(offline["id"], "execution")["base_recipe_version_id"] == VERSION
    assert len(calls) == 3

    execution_id, fence = store.claim("test-worker", 60, 1, execution_id=created["id"])
    candidate = store.publish_candidate(
        execution_id, "test-worker", fence, 1, content=base["selected_version"]["content"],
        recipe_id=RECIPE, base_id=VERSION, operation_key="updated-candidate",
    )
    running = client.task_get(execution_id)
    assert running["recipe_id"] == RECIPE
    assert running["recipe_version_id"] is None
    assert running["recipe_zip_path"] is None
    store.complete(
        execution_id, "test-worker", fence, {
            "input_revision": 1, "recipe_id": RECIPE, "version_id": candidate["id"],
            "test": {"status": "passed"}, "rubrics_version_id": "version_rubrics",
        }, freeze_task_artifact=True,
    )
    assert client.recipes_upsert(execution_id, idempotency_key="publish-update") == {
        "recipe_id": RECIPE, "recipe_version_id": NEXT_VERSION,
    }

    assert client.task_get(execution_id)["recipe_version_id"] == NEXT_VERSION
    reopened = client.execution_create(**{
        **request, "idempotency_key": "cached-explicit", "recipe_reference": {
            **request["recipe_reference"], "recipe_version_id": VERSION,
        },
    })
    assert store.get(reopened["id"], "execution")["base_recipe_version_id"] == VERSION
    assert len(calls) == 4


def test_bootstrap_pinned_selection_requires_current_published_version(tmp_path, monkeypatch):
    client, request, archive = _setup(tmp_path, monkeypatch, shared_headers=False)
    calls = _cloud(monkeypatch, archive, active=NEXT_VERSION, authorization="Bearer fixture-cloud-api-key")
    request["recipe_reference"]["recipe_version_id"] = VERSION
    with pytest.raises(Conflict, match="no longer active"):
        client.execution_create(**request)
    assert calls == [("GET", f"/api/v1/recipes/{RECIPE}", VERSION)]
    assert client.task_list() == []
    assert client.recipe_list() == []

    calls = _cloud(monkeypatch, archive, authorization="Bearer fixture-cloud-api-key")
    created = client.execution_create(**request)
    assert calls[0][2] == VERSION
    assert client._queue.store.get(created["id"], "execution")["base_recipe_version_id"] == VERSION


def test_concurrent_bootstrap_reuses_installed_version_and_submission(tmp_path, monkeypatch):
    client, request, archive = _setup(tmp_path, monkeypatch)
    _cloud(monkeypatch, archive, metadata_barrier=Barrier(2))
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.execution_create, **request)
        second = executor.submit(client.execution_create, **request)
        assert first.result(timeout=10)["id"] == second.result(timeout=10)["id"]

    assert len(client.task_list()) == 1
    assert client.recipe_get(RECIPE)["active_version_id"] == VERSION
    with client._queue.store.connect() as conn:
        assert conn.execute("SELECT id FROM versions").fetchall()[0]["id"] == VERSION
        assert conn.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1


def test_bootstrap_does_not_install_missing_version_during_same_recipe_upload(tmp_path, monkeypatch):
    client, request, archive = _setup(tmp_path, monkeypatch)
    _cloud(monkeypatch, archive)
    initial = client.execution_create(**request)
    store = client._queue.store
    with store.transaction() as conn:
        store.remember(conn, PENDING, RECIPE, {"cloud_recipe_id": RECIPE}, {})

    _cloud(monkeypatch, archive, selected=NEXT_VERSION, active=NEXT_VERSION)
    with pytest.raises(Conflict, match="cloud Recipe target is reserved"):
        client.execution_create(**{
            **request,
            "idempotency_key": "pinned-next",
            "recipe_reference": {**request["recipe_reference"], "recipe_version_id": NEXT_VERSION},
        })

    assert [row["task_id"] for row in client.task_list()] == [initial["id"]]
    assert client.recipe_get(RECIPE)["active_version_id"] == VERSION
    with pytest.raises(NotFound):
        client.recipe_get(RECIPE, version=NEXT_VERSION)


def test_explicitly_pinned_approved_local_version_remains_usable_after_archival(tmp_path, monkeypatch):
    client, request, _ = _setup(tmp_path, monkeypatch)
    path = tmp_path / "payload" / "RECIPE.md"
    recipe = client._queue.recipe_insert(*read_recipe_file(path))
    recipe_id, version_id = recipe["id"], recipe["active_version_id"]
    request["recipe_reference"] = {"path": str(path), "recipe_id": recipe_id, "recipe_version_id": version_id}
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **_: pytest.fail("unexpected cloud request"))
    original = client.execution_create(**request)
    store = client._queue.store
    execution_id, fence = store.claim("test-worker", 60, 1, execution_id=original["id"])
    store.complete(execution_id, "test-worker", fence, {
        "input_revision": 1, "recipe_id": recipe_id, "version_id": version_id,
        "test": {"status": "passed"}, "rubrics_version_id": "version_rubrics",
    })
    with store.transaction() as conn:
        current = store.get_in(conn, recipe_id, "recipe")
        new = store.append_version(conn, current, recipe["selected_version"]["content"])
        assert store.publish_recipe_version(conn, recipe_id, new["id"])

    execution = client.execution_create(**{**request, "idempotency_key": "historical-local"})
    assert store.get(execution["id"], "execution")["base_recipe_version_id"] == version_id
    assert client.recipe_get(recipe_id)["active_version_id"] == new["id"]


@pytest.mark.parametrize(("options", "error", "message", "calls"), [
    ({"sha256": "0" * 64}, Conflict, "SHA-256", 3),
    ({"published": "staged"}, InvalidState, "verified published ZIP", 1),
])
def test_invalid_cloud_version_leaves_no_local_task_or_recipe(tmp_path, monkeypatch, options, error, message, calls):
    client, request, archive = _setup(tmp_path, monkeypatch)
    seen = _cloud(monkeypatch, archive, **options)
    with pytest.raises(error, match=message):
        client.execution_create(**request)
    assert len(seen) == calls
    assert client.task_list() == []
    assert client.recipe_list() == []
    assert not (client.data_dir / "recipes" / RECIPE / VERSION).exists()


def test_explicit_recipe_version_is_local_only_and_requires_recipe_id(tmp_path, monkeypatch):
    client, request, archive = _setup(tmp_path, monkeypatch)
    request["recipe_reference"]["recipe_version_id"] = VERSION
    with pytest.raises(ValueError, match="requires local mode"):
        CloudRSIClient().execution_create(**request)
    del request["recipe_reference"]["recipe_id"]
    with pytest.raises(ValueError, match="requires recipe_id"):
        client.execution_create(**request)


def test_missing_local_recipe_without_cosens_credentials_keeps_not_found(tmp_path, monkeypatch):
    client, request, _ = _setup(tmp_path, monkeypatch, shared_headers=False)
    monkeypatch.delenv("COSENS_API_KEY")
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **_: pytest.fail("unexpected cloud request"))

    with pytest.raises(NotFound, match="recipe not found locally; configure CoSens credentials"):
        client.execution_create(**request)

    assert client.task_list() == []


def test_missing_local_recipe_requires_rsi_gateway(tmp_path, monkeypatch):
    client, request, _ = _setup(tmp_path, monkeypatch, shared_headers=False)
    monkeypatch.delenv("COSENS_API_KEY")
    monkeypatch.delenv("COSENS_RSI_BASE_URL")
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **_: pytest.fail("unexpected cloud request"))

    with pytest.raises(ValueError, match=r"config\.rsi_base_url or COSENS_RSI_BASE_URL"):
        client.execution_create(**request)
    assert client.task_list() == []
