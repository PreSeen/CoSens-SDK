"""Local Recipe uploads adopt formal cloud identities and retain immutable receipts."""

import hashlib
import io
import json
import sqlite3
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

from cosens.rsi import CloudRSIClient, Conflict, InvalidState, NotFound, RSIClient, cloud
from cosens.rsi.models import RSIError
from cosens.rsi.recipe_file import read_recipe_file
from cosens.rsi.recipe_sync import upload_recipe
from cosens.rsi.store import Store

MARKDOWN = "---\nname: test-recipe\ndescription: Evaluate data\n---\n\nInstructions\n"
V1 = {"recipe_id": "recipe_cloud", "recipe_version_id": "version_cloud_1"}
V2 = {"recipe_id": "recipe_cloud", "recipe_version_id": "version_cloud_2"}
RSI_URL = "https://rsi.example/api"


def _fixture_recipe(tmp_path, **options):
    client = RSIClient(data_dir=tmp_path, **options)
    path = tmp_path / "RECIPE.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    first = client._queue.recipe_insert(*read_recipe_file(path))
    return client, first


def _publishing_client(data_dir, *, headers=None):
    return RSIClient(
        data_dir=data_dir, config={
            "headers": {"Authorization": "Bearer fixture-key"} if headers is None else headers,
        },
    )


def _legacy_fixture_recipe(tmp_path, monkeypatch):
    with monkeypatch.context() as legacy:
        legacy.setattr(Store, "_migrate_task_recipes", lambda self, conn: None)
        return _fixture_recipe(tmp_path)


def _zip(path, markdown=MARKDOWN, *, resources=None):
    files = {"RECIPE.md": markdown, **(resources or {})}
    with ZipFile(path, "w") as package:
        for name, contents in files.items():
            package.writestr(name, contents)
        package.writestr(
            "manifest.json",
            json.dumps({"entrypoint": "RECIPE.md", "files": sorted(files)}),
        )
    return path


def _local_new_version(client, recipe):
    store = client._queue.store
    markdown = MARKDOWN.replace("Instructions", "Updated instructions")
    content = {
        **recipe["selected_version"]["content"],
        "instructions": "Updated instructions\n",
    }
    with store.transaction() as conn:
        current = store.get_in(conn, recipe["id"], "recipe")
        version = store.append_version(conn, current, content, recipe_markdown=markdown)
        assert store.publish_recipe_version(conn, recipe["id"], version["id"])
    return version["id"], markdown


def _transport(monkeypatch, handler, *, authorization="Bearer fixture-key"):
    calls = []

    def receive(request):
        assert request.method == "POST" and request.url.path == "/api/recipes_upsert"
        assert request.headers["Authorization"] == authorization
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode()
            + request.content
        )
        fields = {
            part.get_param("name", header="content-disposition"): part.get_payload(
                decode=True
            )
            for part in message.iter_parts()
        }
        calls.append((request.headers["Idempotency-Key"], fields))
        return handler(request, calls)

    transport = httpx.MockTransport(receive)
    monkeypatch.setattr(
        cloud, "CloudRSIClient", lambda **kw: CloudRSIClient(transport=transport, **kw)
    )
    return calls


def _upload(client, recipe_id, version_id, path, **kw):
    filename, data = cloud._read_recipe_zip(path)
    key = kw.pop("idempotency_key", None)
    service = cloud.CloudRSIClient(
        _shared_headers=client._upload_headers,
        **{"api_key": "fixture-key", "base_url": "https://rsi.example/api", **kw},
    )
    try:
        return upload_recipe(client._queue.store, service, filename, data, recipe_id, version_id, key)
    finally:
        service.close()


def _completed_execution(client, recipe_id, version_id):
    store = client._queue.store
    with store.transaction() as conn:
        return store.insert(
            conn,
            "execution",
            {
                "recipe_id": recipe_id,
                "recipe_version_id": version_id,
                "base_recipe_version_id": version_id,
                "publication_base_version_id": version_id,
                "status": "completed",
                "stage": "done",
                "progress_percent": 100,
                "input_revision": 1,
                "material_ids": [],
                "questions": [],
                "answers": {},
                "test_request": {"recipe_id": recipe_id, "version_id": version_id},
                "test_results": [
                    {
                        "id": "historic-test",
                        "recipe_id": recipe_id,
                        "version_id": version_id,
                    }
                ],
                "result": {
                    "test_status": "passed",
                    "published": True,
                    "provisional": False,
                },
                "error": None,
            },
        )["id"]


def test_local_upload_uses_upload_headers(tmp_path, monkeypatch):
    monkeypatch.delenv("COSENS_API_KEY", raising=False)
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    config = {
        "auto_start": False,
        "headers": {
            "Authorization": "Bearer fixture-access-token",
            "X-Auth-Type": "access_token",
            "X-Virtual-User-Id": "vu_fixture",
            "Content-Type": "application/json",
            "Content-Length": "0",
            "Idempotency-Key": "model-key",
        },
    }
    client, recipe = _fixture_recipe(
        tmp_path, model_id="fixture", api_key="fixture-model-key",
        base_url="https://model.example/v1", config=config,
    )
    task_id = _completed_execution(client, recipe["id"], recipe["active_version_id"])
    path = Path(client.task_get(task_id)["artifact"]["zip_path"])

    def receive(request, calls):
        assert request.url.host == "rsi.example"
        assert request.headers["X-Auth-Type"] == "access_token"
        assert request.headers["X-Virtual-User-Id"] == "vu_fixture"
        assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        assert calls[-1] == ("upload-key", {"file": path.read_bytes()})
        return httpx.Response(200, json=V1)

    calls = _transport(monkeypatch, receive, authorization="Bearer fixture-access-token")
    assert client.recipes_upsert(task_id, idempotency_key="upload-key") == V1
    tenant_only = _publishing_client(tmp_path, headers={"X-Virtual-User-Id": "vu_fixture"})
    assert tenant_only.recipes_upsert(task_id=task_id, idempotency_key="upload-key") == V1
    different_tenant = _publishing_client(tmp_path, headers={"X-Virtual-User-Id": "vu_other"})
    with pytest.raises(Conflict):
        different_tenant.recipes_upsert(task_id, idempotency_key="upload-key")
    assert len(calls) == 1


def test_local_upload_promotes_primary_keys_and_replays_old_version(
    tmp_path, monkeypatch
):
    client, first = _fixture_recipe(tmp_path)
    original_recipe, original_version = first["id"], first["active_version_id"]
    execution_id = _completed_execution(client, original_recipe, original_version)
    directory = tmp_path / first["storage_path"]
    (directory / original_version / "resources").mkdir()
    (directory / original_version / "resources" / "input.txt").write_text("sample")
    first_zip = _zip(tmp_path / "initial.zip", resources={"resources/input.txt": "sample"})
    calls = _transport(
        monkeypatch,
        lambda _, seen: httpx.Response(200, json=V1 if len(seen) == 1 else V2),
    )

    assert (
        _upload(
            client,
            original_recipe,
            original_version,
            first_zip.as_uri(),
            idempotency_key="first",
        )
        == V1
    )
    assert "recipe_id" not in calls[0][1]
    assert calls[0][1]["file"] == first_zip.read_bytes()
    assert (
        directory / V1["recipe_version_id"] / "resources" / "input.txt"
    ).read_text() == "sample"
    assert (directory / original_version / "RECIPE.md").read_text() == MARKDOWN

    result = client.execution_get(execution_id)
    assert (result["recipe_id"], result["recipe_version_id"]) == (
        V1["recipe_id"],
        V1["recipe_version_id"],
    )
    assert result["test_results"] == [
        {
            "id": "historic-test",
            "recipe_id": V1["recipe_id"],
            "version_id": V1["recipe_version_id"],
        }
    ]
    via_alias = client.recipe_get(original_recipe, version=original_version)
    assert via_alias["id"] == V1["recipe_id"]
    assert via_alias["storage_path"] == first["storage_path"]
    assert via_alias["selected_version"]["id"] == V1["recipe_version_id"]
    assert client.recipe_list(query={"recipe_ids": [original_recipe]})[0][
        "execution_ids"
    ] == [execution_id]

    next_id, markdown = _local_new_version(client, via_alias)
    second_zip = _zip(tmp_path / "second.zip", markdown)
    assert _upload(client, V1["recipe_id"], next_id, second_zip) == V2
    assert calls[1][1]["recipe_id"] == b"recipe_cloud"
    third_id, third_markdown = _local_new_version(client, via_alias)
    third_zip = _zip(tmp_path / "third.zip", third_markdown)
    with pytest.raises(
        Conflict, match="already linked to a different cloud destination"
    ):
        _upload(
            client,
            V1["recipe_id"],
            third_id,
            third_zip,
            virtual_user_id="another-account",
        )

    reopened = RSIClient(data_dir=tmp_path)
    assert _upload(reopened, original_recipe, original_version, first_zip) == V1
    assert _upload(reopened, V1["recipe_id"], V1["recipe_version_id"], first_zip) == V1
    assert len(calls) == 2
    current = reopened.recipe_get(V2["recipe_id"])
    assert current["active_version_id"] == third_id
    assert "cloud_recipe_id" not in current
    assert (
        reopened.recipe_get(V2["recipe_id"], version=next_id)["selected_version"]["id"]
        == V2["recipe_version_id"]
    )
    with pytest.raises(Conflict, match="different ZIP, key or destination"):
        _upload(
            reopened,
            original_recipe,
            original_version,
            first_zip,
            virtual_user_id="another-account",
        )

    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("SELECT id FROM recipe_objects").fetchall() == [(V1["recipe_id"],)]
        assert conn.execute("SELECT id, recipe_id, recipe_version_id FROM recipes").fetchall() == [
            (execution_id, V1["recipe_id"], V1["recipe_version_id"]),
        ]
        assert conn.execute(
            "SELECT id, recipe_id FROM versions ORDER BY number"
        ).fetchall() == [
            (V1["recipe_version_id"], V1["recipe_id"]),
            (V2["recipe_version_id"], V1["recipe_id"]),
            (third_id, V1["recipe_id"]),
        ]
        assert conn.execute(
            "SELECT base_recipe_version_id FROM executions"
        ).fetchone() == (V1["recipe_version_id"],)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_upload_recovers_ambiguous_failure_with_staged_idempotency_key(
    tmp_path, monkeypatch
):
    client, first = _fixture_recipe(tmp_path)
    recipe_id, version_id = first["id"], first["active_version_id"]
    package = _zip(tmp_path / "base.zip")
    next_id, markdown = _local_new_version(client, first)
    later = _zip(tmp_path / "later.zip", markdown)

    def receive(request, calls):
        if len(calls) == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=V1)

    calls = _transport(monkeypatch, receive)
    with pytest.raises(RSIError, match="could not be reached"):
        _upload(client, recipe_id, version_id, package)
    assert client.recipe_get(recipe_id)["id"] == recipe_id
    reopened = RSIClient(data_dir=tmp_path)
    with pytest.raises(Conflict, match="same version, ZIP and idempotency key"):
        _upload(reopened, recipe_id, next_id, later)
    with pytest.raises(Conflict, match="same version, ZIP and idempotency key"):
        _upload(
            reopened,
            recipe_id,
            version_id,
            package,
            base_url="https://another.example/api",
        )
    with pytest.raises(Conflict, match="same version, ZIP and idempotency key"):
        _upload(
            reopened, recipe_id, version_id, package, virtual_user_id="another-account"
        )
    assert (
        _upload(
            reopened,
            recipe_id,
            version_id,
            package,
            base_url="https://rsi.example/api/",
        )
        == V1
    )
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_definitive_rejection_does_not_bind_recipe_and_allows_retry(
    tmp_path, monkeypatch
):
    client, first = _fixture_recipe(tmp_path)
    recipe_id, version_id = first["id"], first["active_version_id"]
    package = _zip(tmp_path / "base.zip")
    _transport(
        monkeypatch,
        lambda _, calls: (
            httpx.Response(422, json={"error": {"code": "invalid_request"}})
            if len(calls) == 1
            else httpx.Response(200, json=V1)
        ),
    )
    with pytest.raises(ValueError):
        _upload(client, recipe_id, version_id, package, idempotency_key="rejected-key")
    assert client.recipe_get(recipe_id)["id"] == recipe_id
    assert (
        _upload(client, recipe_id, version_id, package, idempotency_key="fixed-key")
        == V1
    )


@pytest.mark.parametrize("schema_version", [7, 9])
def test_old_schema_migrates_without_changing_unpublished_recipe(tmp_path, monkeypatch, schema_version):
    _, first = _legacy_fixture_recipe(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        conn.execute(f"PRAGMA user_version={schema_version}")
    reopened = RSIClient(data_dir=tmp_path)
    assert reopened.recipe_get(first["id"]) == first
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (10,)


def test_existing_cloud_schema_keeps_recipe_and_version_tables(tmp_path, monkeypatch):
    _, first = _legacy_fixture_recipe(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        conn.execute("CREATE TABLE cloud_recipe_account (account_id TEXT NOT NULL)")

    reopened = RSIClient(data_dir=tmp_path)
    assert reopened.recipe_get(first["id"]) == first
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (9,)
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='versions'").fetchone() == ("table",)


@pytest.mark.parametrize("pruned", [False, True])
def test_schema_9_backfills_task_artifact_without_repackaging(tmp_path, monkeypatch, pruned):
    client, first = _legacy_fixture_recipe(tmp_path, monkeypatch)
    pair = (first["id"], first["active_version_id"])
    task_id = _completed_execution(client, *pair)
    leaf_id = _completed_execution(client, *pair)
    with client._queue.store.transaction() as conn:
        store = client._queue.store
        root = store.get_in(conn, task_id, "execution")
        leaf = store.get_in(conn, leaf_id, "execution")
        store.save(conn, {
            **root, "status": "cancelled", "stage": "cancelled", "result": None,
            "continuation_execution_id": leaf_id,
        })
        store.save(conn, {**leaf, "base_execution_id": task_id})
    existing = client.task_get(task_id)["artifact"]
    existing_path = Path(existing["zip_path"])
    zip_bytes = existing_path.read_bytes()
    if pruned:
        existing_path.unlink()

    reopened = RSIClient(data_dir=tmp_path)
    [entry] = reopened.task_list()
    assert (entry["task_id"], entry["execution_id"]) == (task_id, leaf_id)
    if pruned:
        assert not existing_path.exists()
    else:
        migrated = reopened.task_get(task_id)
        assert migrated["artifact"] == existing
        assert migrated["execution_ids"] == [task_id, leaf_id]
        assert existing_path.read_bytes() == zip_bytes
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("SELECT * FROM recipes").fetchall() == [
            (task_id, *pair, None if pruned else f"task-packages/{task_id}.zip"),
        ]
        assert conn.execute("SELECT type FROM sqlite_master WHERE name='versions'").fetchone() == ("view",)


def test_schema_8_adopts_already_uploaded_ids(tmp_path, monkeypatch):
    client, first = _legacy_fixture_recipe(tmp_path, monkeypatch)
    recipe_id, version_id = first["id"], first["active_version_id"]
    execution_id = _completed_execution(client, recipe_id, version_id)
    package = _zip(tmp_path / "base.zip")
    plan = {
        "base_url": "https://rsi.example/api",
        "virtual_user_id": None,
        "local_recipe_id": recipe_id,
        "local_recipe_version_id": version_id,
        "zip_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "idempotency_key": "v8-key",
        "cloud_recipe_id": None,
    }
    with client._queue.store.transaction() as conn:
        conn.execute("ALTER TABLE recipes ADD COLUMN cloud_recipe_id TEXT")
        conn.execute("ALTER TABLE recipes ADD COLUMN cloud_recipe_version_id TEXT")
        conn.execute("ALTER TABLE versions ADD COLUMN cloud_recipe_version_id TEXT")
        conn.execute(
            "UPDATE recipes SET cloud_recipe_id=?,cloud_recipe_version_id=? WHERE id=?",
            (V1["recipe_id"], V1["recipe_version_id"], recipe_id),
        )
        conn.execute(
            "UPDATE versions SET cloud_recipe_version_id=? WHERE id=?",
            (V1["recipe_version_id"], version_id),
        )
        client._queue.store.remember(
            conn, "recipe_cloud_upload_receipt", version_id, plan, V1
        )
        conn.execute("PRAGMA user_version=8")
    reopened = RSIClient(data_dir=tmp_path)
    assert reopened.recipe_get(recipe_id, version=version_id)["id"] == V1["recipe_id"]
    assert (
        reopened.execution_get(execution_id)["recipe_version_id"]
        == V1["recipe_version_id"]
    )
    calls = _transport(
        monkeypatch, lambda _, __: pytest.fail("migration issued an upload")
    )
    assert _upload(reopened, recipe_id, version_id, package) == V1
    assert calls == []
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (10,)
        assert conn.execute("SELECT id FROM recipe_objects").fetchall() == [(V1["recipe_id"],)]
        assert conn.execute("SELECT id, recipe_id, recipe_version_id FROM recipes").fetchall() == [
            (execution_id, V1["recipe_id"], V1["recipe_version_id"]),
        ]
        assert conn.execute("SELECT id FROM versions").fetchall() == [
            (V1["recipe_version_id"],)
        ]
        for table in ("recipes", "versions"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert not {"cloud_recipe_id", "cloud_recipe_version_id"} & columns


def test_cloud_receipt_cannot_bind_two_local_recipes(tmp_path, monkeypatch):
    client, first = _fixture_recipe(tmp_path)
    second = client._queue.recipe_insert(*read_recipe_file(tmp_path / "RECIPE.md"))
    package = _zip(tmp_path / "base.zip")
    _transport(monkeypatch, lambda _, __: httpx.Response(200, json=V1))
    assert _upload(client, first["id"], first["active_version_id"], package) == V1
    with pytest.raises(
        Conflict, match="already linked to another local Recipe or version"
    ):
        _upload(client, second["id"], second["active_version_id"], package)
    assert client.recipe_get(second["id"])["id"] == second["id"]


def test_upload_replays_same_reservation_if_execution_starts_during_network_call(
    tmp_path, monkeypatch
):
    client, first = _fixture_recipe(tmp_path)
    recipe_id, version_id = first["id"], first["active_version_id"]
    execution_id = _completed_execution(client, recipe_id, version_id)
    store = client._queue.store
    package = _zip(tmp_path / "base.zip")

    def receive(_, seen):
        if len(seen) == 1:
            with store.transaction() as conn:
                conn.execute(
                    "UPDATE executions SET status='running' WHERE id=?", (execution_id,)
                )
        return httpx.Response(200, json=V1)

    calls = _transport(monkeypatch, receive)
    with pytest.raises(Conflict, match="active local Recipe execution"):
        _upload(client, recipe_id, version_id, package)
    assert client.recipe_get(recipe_id)["id"] == recipe_id
    with pytest.raises(Conflict, match="active local Recipe execution"):
        _upload(client, recipe_id, version_id, package)
    assert len(calls) == 1

    with store.transaction() as conn:
        conn.execute(
            "UPDATE executions SET status='completed' WHERE id=?", (execution_id,)
        )
    assert _upload(client, recipe_id, version_id, package) == V1
    assert calls[0] == calls[1]
    assert client.execution_get(execution_id)["recipe_id"] == V1["recipe_id"]


def test_upload_restores_pruned_version_entrypoint(tmp_path, monkeypatch):
    from cosens.rsi.recipe_file import render_recipe_markdown

    client, recipe = _fixture_recipe(tmp_path)
    version_id = recipe["active_version_id"]
    directory = tmp_path / recipe["storage_path"]
    original = directory / version_id
    (original / "helper.txt").write_text("keep this")
    (original / "RECIPE.md").unlink()
    package = _zip(
        tmp_path / "base.zip",
        render_recipe_markdown(recipe["selected_version"]["content"]),
        resources={"helper.txt": "keep this"},
    )
    _transport(monkeypatch, lambda _, __: httpx.Response(200, json=V1))

    assert _upload(client, recipe["id"], version_id, package) == V1
    formal = directory / V1["recipe_version_id"]
    assert (formal / "helper.txt").read_text() == "keep this"
    assert (formal / "RECIPE.md").read_text() == (
        '---\nname: "test-recipe"\ndescription: "Evaluate data"\n---\n\nInstructions\n'
    )


def test_upload_rejects_different_version_bytes_before_network(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    wrong = _zip(tmp_path / "wrong.zip", MARKDOWN.replace("Instructions", "Different"))
    calls = _transport(monkeypatch, lambda *_: pytest.fail("unexpected upload"))

    with pytest.raises(ValueError, match="local Recipe version"):
        _upload(client, recipe["id"], recipe["active_version_id"], wrong)

    assert calls == []
    assert client.recipe_get(recipe["id"]) == recipe


def test_worker_candidate_requires_approved_execution_before_upload(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    recipe_id = recipe["id"]
    store = client._queue.store
    with store.transaction() as conn:
        candidate = store.append_version(
            conn, recipe, recipe["selected_version"]["content"], recipe_markdown=MARKDOWN,
            provenance={"execution_id": "execution_unapproved"},
        )
    package = _zip(tmp_path / "candidate.zip")
    calls = _transport(monkeypatch, lambda *_: pytest.fail("unexpected upload"))

    with pytest.raises(InvalidState, match="approved final Task"):
        _upload(client, recipe_id, candidate["id"], package)
    assert calls == []


def test_task_final_zip_survives_reload_and_cloud_identity_adoption(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    pair = (recipe["id"], recipe["active_version_id"])
    execution_id = _completed_execution(client, *pair)
    directory = tmp_path / recipe["storage_path"] / recipe["active_version_id"]
    (directory / "PACKAGE.zip").write_bytes(b"version resource")
    task = client.task_get(execution_id)
    artifact = task["artifact"]
    assert task["task_id"] == execution_id
    assert (task["recipe_id"], task["recipe_version_id"]) == pair
    expected = {
        "zip_path": str(tmp_path / "task-packages" / f"{execution_id}.zip"),
        "recipe_id": pair[0],
        "recipe_version_id": pair[1],
    }
    assert artifact == expected
    assert Path(task["recipe_zip_path"]).is_absolute()
    assert task["recipe_zip_path"] == artifact["zip_path"]
    with ZipFile(artifact["zip_path"]) as package:
        assert package.read("RECIPE.md") == MARKDOWN.encode()
        assert package.read("PACKAGE.zip") == b"version resource"
    frozen_file = Path(artifact["zip_path"])
    frozen_bytes = frozen_file.read_bytes()
    frozen_file.write_bytes(b"tampered")
    with pytest.raises(Conflict, match="final Recipe ZIP contents changed"):
        client.task_get(execution_id)
    frozen_file.write_bytes(frozen_bytes)

    calls = _transport(monkeypatch, lambda *_: httpx.Response(200, json=V1))
    repacked = _zip(tmp_path / "repacked.zip", resources={"PACKAGE.zip": b"version resource"})
    with pytest.raises(Conflict, match="frozen Task archive"):
        _upload(client, recipe["id"], recipe["active_version_id"], repacked)
    assert calls == []
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    assert _publishing_client(tmp_path).recipes_upsert(execution_id, idempotency_key="upload-task") == V1
    assert len(calls) == 1
    reopened = RSIClient(data_dir=tmp_path)
    final = reopened.task_get(execution_id)
    assert final["recipe_id"] == V1["recipe_id"]
    assert final["recipe_version_id"] == V1["recipe_version_id"]
    assert final["artifact"] == {**V1, "zip_path": artifact["zip_path"]}
    assert final["recipe_zip_path"] == final["artifact"]["zip_path"]
    with pytest.raises(InvalidState, match="already has a final Recipe"):
        reopened.task_restart(execution_id, idempotency_key="restart-final")
    frozen_file.unlink()
    cloud_only = reopened.task_get(execution_id)
    assert cloud_only["artifact"] is None
    assert cloud_only["recipe_id"] == V1["recipe_id"]
    assert cloud_only["recipe_version_id"] == V1["recipe_version_id"]
    assert cloud_only["recipe_zip_path"] is None
    monkeypatch.delenv("COSENS_API_KEY", raising=False)
    assert reopened.recipes_upsert(execution_id, idempotency_key="upload-task") == V1
    assert reopened.recipes_upsert(execution_id, recipe_id=V1["recipe_id"], idempotency_key="upload-task") == V1
    assert reopened.recipes_upsert(task_id=execution_id) == V1
    with pytest.raises(Conflict, match="receipt for a different cloud Recipe"):
        reopened.recipes_upsert(execution_id, recipe_id="recipe_other", idempotency_key="upload-task")
    with pytest.raises(Conflict, match="different ZIP, key or destination"):
        reopened.recipes_upsert(execution_id, idempotency_key="different-key")
    monkeypatch.setenv("COSENS_RSI_BASE_URL", "https://other.example/api")
    with pytest.raises(Conflict, match="different ZIP, key or destination"):
        reopened.recipes_upsert(execution_id, idempotency_key="upload-task")
    assert len(calls) == 1


def test_public_local_upload_rejects_provisional_task_and_old_recipe_pair(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    version_id = recipe["active_version_id"]
    task_id = _completed_execution(client, recipe["id"], version_id)
    store = client._queue.store
    with store.transaction() as conn:
        execution = store.get_in(conn, task_id, "execution")
        store.save(conn, {**execution, "result": {**execution["result"], "provisional": True}})
    provisional = client.task_get(task_id)
    assert provisional["recipe_id"] == recipe["id"]
    assert provisional["recipe_version_id"] is None
    assert provisional["recipe_zip_path"] is None
    assert provisional["artifact"] is None
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **_: pytest.fail("unexpected cloud request"))

    with pytest.raises(InvalidState, match="approved final Task Recipe"):
        client.recipes_upsert(task_id)
    with pytest.raises(TypeError):
        client.recipes_upsert(recipe["id"], version_id)
    with pytest.raises(TypeError, match="recipe_version_id"):
        client.recipes_upsert(recipe_id=recipe["id"], recipe_version_id=version_id)
    with pytest.raises(NotFound, match="execution not found"):
        client.recipes_upsert(recipe["id"])


def test_local_upload_requires_configured_headers_before_creating_pending_request(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    task_id = _completed_execution(client, recipe["id"], recipe["active_version_id"])
    monkeypatch.setenv("COSENS_API_KEY", "unused-cosens-fallback")
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    monkeypatch.setattr(cloud, "CloudRSIClient", lambda **_: pytest.fail("unexpected cloud client"))

    with pytest.raises(InvalidState, match=r"config\.headers"):
        client.recipes_upsert(task_id)
    for field in ("api_key", "base_url", "virtual_user_id"):
        with pytest.raises(TypeError, match=field):
            client.recipes_upsert(task_id, **{field: "unsupported"})

    with client._queue.store.connect() as conn:
        assert not conn.execute("SELECT 1 FROM idempotency WHERE scope='recipe_cloud_upload_pending'").fetchone()


def test_header_only_local_client_keeps_model_endpoint_validation(tmp_path):
    headers = {"Authorization": "Bearer mock-cloud"}
    assert RSIClient(data_dir=tmp_path, config={"headers": headers}).mode == "local"
    with pytest.raises(ValueError, match="model_id"):
        RSIClient(data_dir=tmp_path, config={"headers": headers, "timeout": 2})
    with pytest.raises(ValueError, match="base_url"):
        RSIClient(data_dir=tmp_path, config={"headers": headers}, model_id="incomplete")


def test_local_upload_selects_requested_task_after_newer_version_becomes_active(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    recipe_id, old_version = recipe["id"], recipe["active_version_id"]
    task_id = _completed_execution(client, recipe_id, old_version)
    new_version, _ = _local_new_version(client, recipe)
    newer_task_id = _completed_execution(client, recipe_id, new_version)
    assert client.task_get(newer_task_id)["recipe_version_id"] == new_version
    calls = _transport(monkeypatch, lambda *_: httpx.Response(200, json=V1))

    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    assert _publishing_client(tmp_path).recipes_upsert(task_id) == V1

    with ZipFile(io.BytesIO(calls[0][1]["file"])) as package:
        assert package.read("RECIPE.md") == MARKDOWN.encode()
    assert client.recipe_get(recipe_id)["active_version_id"] == new_version
    with client._queue.store.connect() as conn:
        newer_task = conn.execute(
            "SELECT recipe_id, recipe_version_id FROM recipes WHERE id=?",
            (newer_task_id,),
        ).fetchone()
    assert tuple(newer_task) == (V1["recipe_id"], new_version)


def test_local_upload_rejects_a_legacy_recipe_without_a_task(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    pair = (recipe["id"], recipe["active_version_id"])
    calls = _transport(monkeypatch, lambda *_: httpx.Response(200, json=V1))
    assert _upload(client, *pair, _zip(tmp_path / "legacy.zip")) == V1
    assert client.task_list() == []
    monkeypatch.delenv("COSENS_API_KEY", raising=False)
    with pytest.raises(NotFound, match="execution not found"):
        client.recipes_upsert(V1["recipe_id"])
    assert len(calls) == 1


def test_local_upload_retries_task_with_original_frozen_zip_after_timeout(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    pair = (recipe["id"], recipe["active_version_id"])
    task_id = _completed_execution(client, *pair)

    def receive(request, calls):
        if len(calls) == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=V1)

    calls = _transport(monkeypatch, receive)
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    with pytest.raises(RSIError, match="could not be reached"):
        _publishing_client(tmp_path).recipes_upsert(task_id, idempotency_key="retry-task")

    assert _publishing_client(tmp_path).recipes_upsert(task_id, idempotency_key="retry-task") == V1
    assert calls[0] == calls[1]


def test_explicit_cloud_target_binds_recipe_and_is_used_by_later_implicit_uploads(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    local_id, local_version = recipe["id"], recipe["active_version_id"]
    task_id = _completed_execution(client, local_id, local_version)
    before = client.task_get(task_id)
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    publishing = _publishing_client(tmp_path)
    calls = _transport(monkeypatch, lambda _, seen: httpx.Response(200, json=V1 if len(seen) == 1 else V2))
    target = V1["recipe_id"]

    assert publishing.recipes_upsert(task_id, recipe_id=target) == V1
    first_key, fields = calls[0]
    assert fields["recipe_id"] == target.encode()
    fingerprint = hashlib.sha256(fields["file"]).hexdigest()
    binding = f"{local_id}\0{local_version}\0{fingerprint}\0{target}"
    assert first_key == "local-recipe-upload-" + hashlib.sha256(binding.encode()).hexdigest()
    after = client.task_get(task_id)
    assert (after["task_id"], after["recipe_id"], after["recipe_version_id"], after["recipe_zip_path"]) == (
        task_id, target, V1["recipe_version_id"], before["recipe_zip_path"],
    )

    other = client._queue.recipe_insert(*read_recipe_file(tmp_path / "RECIPE.md"))
    other_task = _completed_execution(client, other["id"], other["active_version_id"])
    for conflicting in (target, local_id):
        with pytest.raises(Conflict, match="another local Recipe or alias"):
            publishing.recipes_upsert(other_task, recipe_id=conflicting)

    next_version, _ = _local_new_version(client, client.recipe_get(target))
    next_task = _completed_execution(client, target, next_version)
    with pytest.raises(Conflict, match="already linked to a different cloud Recipe"):
        publishing.recipes_upsert(next_task, recipe_id="recipe_other")
    assert publishing.recipes_upsert(next_task) == V2
    assert calls[1][1]["recipe_id"] == target.encode()
    assert len(calls) == 2


def test_explicit_target_reservation_cannot_be_changed_or_claimed_until_retry(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    task_id = _completed_execution(client, recipe["id"], recipe["active_version_id"])
    other = client._queue.recipe_insert(*read_recipe_file(tmp_path / "RECIPE.md"))
    other_task = _completed_execution(client, other["id"], other["active_version_id"])
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    target = V1["recipe_id"]
    publishing = _publishing_client(tmp_path)

    def receive(request, calls):
        if len(calls) == 1:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=V1)

    calls = _transport(monkeypatch, receive)
    with pytest.raises(RSIError, match="could not be reached"):
        publishing.recipes_upsert(task_id, recipe_id=target)
    with pytest.raises(Conflict, match="different cloud Recipe target"):
        publishing.recipes_upsert(task_id, recipe_id="recipe_other")
    with pytest.raises(Conflict, match="reserved by another local Recipe upload"):
        publishing.recipes_upsert(other_task, recipe_id=target)

    assert _publishing_client(tmp_path).recipes_upsert(task_id) == V1
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1]["recipe_id"] == target.encode()


def test_explicit_target_must_match_the_cloud_receipt(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    task_id = _completed_execution(client, recipe["id"], recipe["active_version_id"])
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    _transport(monkeypatch, lambda *_: httpx.Response(200, json={**V1, "recipe_id": "recipe_unexpected"}))

    with pytest.raises(RSIError, match="receipt for a different Recipe"):
        _publishing_client(tmp_path).recipes_upsert(task_id, recipe_id=V1["recipe_id"])
    detail = client.task_get(task_id)
    assert (detail["recipe_id"], detail["recipe_version_id"]) == (recipe["id"], recipe["active_version_id"])


def test_historical_upload_preserves_formal_ids_without_reconstructing_different_zip(tmp_path, monkeypatch):
    client, recipe = _fixture_recipe(tmp_path)
    execution_id = _completed_execution(client, recipe["id"], recipe["active_version_id"])
    legacy_package = _zip(tmp_path / "legacy.zip")
    calls = _transport(monkeypatch, lambda *_: httpx.Response(200, json=V1))
    assert _upload(client, recipe["id"], recipe["active_version_id"], legacy_package) == V1

    reopened = RSIClient(data_dir=tmp_path)
    detail = reopened.task_get(execution_id)
    assert (detail["recipe_id"], detail["recipe_version_id"]) == (V1["recipe_id"], V1["recipe_version_id"])
    assert detail["artifact"] is None
    assert not (tmp_path / "task-packages" / f"{execution_id}.zip").exists()
    monkeypatch.setenv("COSENS_RSI_BASE_URL", RSI_URL)
    assert reopened.recipes_upsert(execution_id) == V1
    assert len(calls) == 1
