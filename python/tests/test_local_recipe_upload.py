"""Local Recipe uploads adopt formal cloud identities and retain immutable receipts."""

import hashlib
import json
import sqlite3
from email import policy
from email.parser import BytesParser
from zipfile import ZipFile

import httpx
import pytest

from cosens.rsi import CloudRSIClient, Conflict, RSIClient, cloud
from cosens.rsi.models import RSIError

MARKDOWN = "---\nname: test-recipe\ndescription: Evaluate data\n---\n\nInstructions\n"
V1 = {"recipe_id": "recipe_cloud", "recipe_version_id": "version_cloud_1"}
V2 = {"recipe_id": "recipe_cloud", "recipe_version_id": "version_cloud_2"}


def _fixture_recipe(tmp_path):
    client = RSIClient(data_dir=tmp_path)
    path = tmp_path / "RECIPE.md"
    path.write_text(MARKDOWN, encoding="utf-8")
    first = client.recipe_insert(path)
    return client, first


def _zip(path, markdown=MARKDOWN):
    with ZipFile(path, "w") as package:
        package.writestr("RECIPE.md", markdown)
        package.writestr(
            "manifest.json",
            json.dumps({"entrypoint": "RECIPE.md", "files": ["RECIPE.md"]}),
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


def _transport(monkeypatch, handler):
    calls = []

    def receive(request):
        assert request.method == "POST" and request.url.path == "/api/recipes_upsert"
        assert request.headers["Authorization"] == "Bearer fixture-key"
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
    return client.recipes_upsert(
        path,
        local_recipe_id=recipe_id,
        local_recipe_version_id=version_id,
        **{"api_key": "fixture-key", "base_url": "https://rsi.example/api", **kw},
    )


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


def test_local_upload_promotes_primary_keys_and_replays_old_version(
    tmp_path, monkeypatch
):
    client, first = _fixture_recipe(tmp_path)
    original_recipe, original_version = first["id"], first["active_version_id"]
    execution_id = _completed_execution(client, original_recipe, original_version)
    directory = tmp_path / first["storage_path"]
    (directory / original_version / "resources").mkdir()
    (directory / original_version / "resources" / "input.txt").write_text("sample")
    first_zip = _zip(tmp_path / "initial.zip")
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
        assert conn.execute("SELECT id FROM recipes").fetchall() == [(V1["recipe_id"],)]
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


def test_schema_7_migrates_without_changing_unpublished_recipe(tmp_path):
    _, first = _fixture_recipe(tmp_path)
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        conn.execute("PRAGMA user_version=7")
    reopened = RSIClient(data_dir=tmp_path)
    assert reopened.recipe_get(first["id"]) == first
    with sqlite3.connect(tmp_path / "rsi.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (9,)


def test_schema_8_adopts_already_uploaded_ids(tmp_path, monkeypatch):
    client, first = _fixture_recipe(tmp_path)
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
        assert conn.execute("PRAGMA user_version").fetchone() == (9,)
        assert conn.execute("SELECT id FROM recipes").fetchall() == [(V1["recipe_id"],)]
        assert conn.execute("SELECT id FROM versions").fetchall() == [
            (V1["recipe_version_id"],)
        ]
        for table in ("recipes", "versions"):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert not {"cloud_recipe_id", "cloud_recipe_version_id"} & columns


def test_cloud_receipt_cannot_bind_two_local_recipes(tmp_path, monkeypatch):
    client, first = _fixture_recipe(tmp_path)
    second = client.recipe_insert(tmp_path / "RECIPE.md")
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
    client, recipe = _fixture_recipe(tmp_path)
    version_id = recipe["active_version_id"]
    directory = tmp_path / recipe["storage_path"]
    original = directory / version_id
    (original / "helper.txt").write_text("keep this")
    (original / "RECIPE.md").unlink()
    package = _zip(tmp_path / "base.zip")
    _transport(monkeypatch, lambda _, __: httpx.Response(200, json=V1))

    assert _upload(client, recipe["id"], version_id, package) == V1
    formal = directory / V1["recipe_version_id"]
    assert (formal / "helper.txt").read_text() == "keep this"
    assert (formal / "RECIPE.md").read_text() == (
        '---\nname: "test-recipe"\ndescription: "Evaluate data"\n---\n\nInstructions\n'
    )
