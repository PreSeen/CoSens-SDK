import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError
from cosens.local import LocalRSI
from cosens.local.publish import UPLOAD_SCHEMA, upload_form
from cosens.local.recipe_stages import RECIPE_MAX_TEXT_CHARS
from cosens.local.store import SCHEMA, canonical_json, fingerprint
from local_rsi_helpers import FakeModel, complete_execution, first_revision, make_rsi, recipe_reply

CONTRACT = Path(__file__).resolve().parents[2] / "contract" / "local-rsi"
RECIPE = {"title": "T", "when": "W", "when_not": "N",
          "steps": [{"id": "s1", "instruction": "I", "evidence_refs": [{"artifact_id": "a"}]}],
          "lessons": [{"when": "W", "do": "D", "why": "Y", "when_not": "N", "evidence_refs": [{"feedback_id": "f"}]}]}
PLAIN = {"title": "Shared", "when": "W", "when_not": "N", "steps": [{"id": "s1", "instruction": "I"}], "lessons": []}


def _error(status, code, chain=None):
    detail = {"code": code, "message": code}
    if chain is not None:
        detail["current_version"] = chain["current"]
    return httpx.Response(status, json={"detail": detail})


class FakeService:
    """Memory recall and the /v1/recipes routes, with the Recipe store's version rules."""

    def __init__(self):
        self.requests, self.uploads, self.memory, self.chains = [], [], {}, {}
        self.fail_status, self.lose_response = None, False

    def add(self, recipe_id, recipe, *, version=1, origin="uploaded"):
        chain = self.chains.setdefault(recipe_id, {"origin": origin, "versions": {}, "current": None})
        chain["versions"][version] = {"recipe": recipe, "sha": fingerprint(recipe), "base_version": chain["current"]}
        chain["current"] = version

    def view(self, recipe_id, version):
        chain = self.chains[recipe_id]
        stored = chain["versions"][version]
        return {"recipe_id": recipe_id, "origin": chain["origin"], "version": version, "base_version": stored["base_version"],
                "current_version": chain["current"], "recipe": stored["recipe"], "recipe_sha256": stored["sha"],
                "task_id": "task_server" if chain["origin"] == "generated" else None, "created_at": "2026-11-13T00:00:00Z"}

    def summary(self, recipe_id):
        chain = self.chains[recipe_id]
        return {"recipe_id": recipe_id, "origin": chain["origin"], "version": chain["current"],
                "title": chain["versions"][chain["current"]]["recipe"]["title"], "task_id": None, "updated_at": "t"}

    def upload(self, recipe_id, body):
        self.uploads.append({"recipe_id": recipe_id, **body})
        version, base, sha = body["version"], body.get("base_version"), fingerprint(body["recipe"])
        chain = self.chains.get(recipe_id)
        if chain is None:
            if base is not None:
                return _error(409, "recipe_version_conflict")
            self.add(recipe_id, body["recipe"], version=version)
            return httpx.Response(201, json=self.view(recipe_id, version))
        if chain["origin"] != "uploaded":
            return _error(409, "recipe_id_unavailable")
        if version in chain["versions"]:
            if chain["versions"][version]["sha"] == sha:
                return httpx.Response(200, json=self.view(recipe_id, version))
            return _error(409, "recipe_version_conflict", chain)
        if version <= chain["current"] or (base is not None and base != chain["current"]):
            return _error(409, "recipe_version_conflict", chain)
        self.add(recipe_id, body["recipe"], version=version)
        return httpx.Response(201, json=self.view(recipe_id, version))

    def __call__(self, request):
        path, query = request.url.path, {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.requests.append(path)
        body = json.loads(request.content or b"{}")
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"detail": "no"})
        if path == "/api/v1/memory/recall":
            return httpx.Response(200, json={"items": [
                {"id": k, "text": v, "revision": 1, "score": 0.9, "kind": "source",
                 "refs": [{"source_id": k, "revision": 1, "content_hash": "h", "start": 0, "end": 1}]}
                for k, v in self.memory.items()]})
        if path == "/api/v1/recipes" and "q" in query:
            words = query["q"].lower().split()
            found = [{**self.summary(k), "score": 0.5} for k in self.chains
                     if any(w in self.summary(k)["title"].lower() for w in words)]
            return httpx.Response(200, json={"items": found[:int(query["limit"])]})
        if path == "/api/v1/recipes":
            items = [self.summary(k) for k in reversed(self.chains) if query.get("origin") in (None, self.chains[k]["origin"])]
            return httpx.Response(200, json={"items": items[:int(query["limit"])], "has_more": False, "next_cursor": None})
        recipe_id = path.removeprefix("/api/v1/recipes/")
        if request.method == "POST":
            response = self.upload(recipe_id, body)
            if self.lose_response:
                return httpx.Response(502, json={"detail": "gateway lost the response"})
            return response
        chain = self.chains.get(recipe_id)
        version = int(query["version"]) if "version" in query else chain and chain["current"]
        if chain is None or version not in chain["versions"]:
            return httpx.Response(404, json={"detail": "Recipe not found"})
        return httpx.Response(200, json=self.view(recipe_id, version))


def setup(tmp_path, *replies, name="s.sqlite3", service=None):
    server = service or FakeService()
    client = CoSensClient(api_key="k", _http_transport=httpx.MockTransport(server))
    rsi, model = make_rsi(tmp_path, *replies, client=client, name=name)
    return rsi, model, server


async def generate(rsi, task, revision_id):
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=revision_id)
    return await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)


async def improve(rsi, task, key):
    started = await rsi.start_execution(task.task_id, key=key)
    done = await complete_execution(rsi, started, key=key + "-out")
    return await generate(rsi, task, done.revision_id)


def test_the_upload_schema_is_the_contract_file():
    assert json.loads((CONTRACT / "recipe-upload.schema.json").read_text()) == UPLOAD_SCHEMA


def test_the_upload_form_drops_evidence_and_checks_the_limits():
    assert upload_form(RECIPE) == {**RECIPE, "steps": [{"id": "s1", "instruction": "I"}],
                                   "lessons": [{"when": "W", "do": "D", "why": "Y", "when_not": "N"}]}
    with pytest.raises(ValueError, match="unique"):
        upload_form({**PLAIN, "steps": [{"id": "s1", "instruction": "I"}] * 2})
    with pytest.raises(ValueError, match=str(RECIPE_MAX_TEXT_CHARS)):
        upload_form({**PLAIN, "steps": [{"id": f"s{i}", "instruction": "I" * 1200}
                                         for i in range(RECIPE_MAX_TEXT_CHARS // 1200 + 1)]})
    with pytest.raises(ValueError, match="upload schema"):
        upload_form({**PLAIN, "extra": "x"})


async def test_each_version_is_uploaded_without_evidence(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    first = await generate(rsi, task, done.revision_id)
    second = await improve(rsi, task, "two")
    assert first.publish_state == second.publish_state == "published"
    assert [(u["recipe_id"], u["version"], u.get("base_version")) for u in server.uploads] == [
        (first.recipe_id, 1, None), (first.recipe_id, 2, 1)]
    assert "evidence_refs" not in json.dumps(server.uploads)
    assert server.chains[first.recipe_id]["versions"][2]["recipe"]["title"] == "V2"


async def test_a_failed_upload_keeps_the_local_version_and_retries_idempotently(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    server.lose_response = True  # stored on the server, but the reply never arrives
    job = await generate(rsi, task, done.revision_id)
    assert job.status == "candidate" and job.current and job.publish_state == "failed"
    server.lose_response = False
    assert (await rsi.publish_recipe(job.recipe_id)).publish_state == "published"
    assert (await rsi.publish_recipe(job.recipe_id)).publish_state == "published"
    assert len(server.uploads) == 2 and server.chains[job.recipe_id]["current"] == 1
    assert (await improve(rsi, task, "two")).publish_state == "published"
    assert server.uploads[-1]["base_version"] == 1


async def test_another_machine_uploading_first_is_a_conflict(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    first = await generate(rsi, task, done.revision_id)
    server.fail_status = 503
    await improve(rsi, task, "two")  # our own V2 never reaches the server
    server.fail_status = None
    server.add(first.recipe_id, PLAIN, version=2)
    job = await improve(rsi, task, "three")
    assert job.current and job.version == 3 and job.publish_state == "publish_conflict"
    assert server.chains[first.recipe_id]["current"] == 2 and server.chains[first.recipe_id]["versions"][2]["recipe"] == PLAIN
    assert rsi._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 1


async def test_an_upload_whose_response_was_lost_is_recognised_by_the_next_version(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"), recipe_reply("V3"))
    task, _, done = await first_revision(rsi)
    first = await generate(rsi, task, done.revision_id)
    server.lose_response = True
    assert (await improve(rsi, task, "two")).publish_state == "failed"
    server.lose_response = False
    job = await improve(rsi, task, "three")
    assert (job.version, job.publish_state) == (3, "published")
    assert sorted(server.chains[first.recipe_id]["versions"]) == [1, 2, 3]
    assert [(u["version"], u.get("base_version")) for u in server.uploads[-2:]] == [(3, 1), (3, 2)]
    assert rsi._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 3


async def test_an_upload_lost_to_a_crash_is_recovered_on_the_next_wait(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done.revision_id)
    server.chains.clear()
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET publish_state='pending'")
        db.execute("UPDATE recipe_heads SET remote_version=NULL")
    assert (await rsi.wait_workflow_job("recipe_candidate", job.recipe_candidate_id)).publish_state == "published"
    assert server.chains[job.recipe_id]["current"] == 1


async def test_search_get_and_continue_an_uploaded_recipe_on_another_machine(tmp_path):
    service = FakeService()
    first, _, _ = setup(tmp_path, recipe_reply("Shared"), name="a.sqlite3", service=service)
    task, _, done = await first_revision(first)
    shared = await generate(first, task, done.revision_id)

    second, model, _ = setup(tmp_path, recipe_reply("Improved"), name="b.sqlite3", service=service)
    found = await second.search_recipes("shared method")
    assert [item.recipe_id for item in found["items"]] == [shared.recipe_id]
    assert [item.recipe_id for item in (await second.list_recipes(origin="uploaded"))["items"]] == [shared.recipe_id]
    view = await second.get_recipe(found["items"][0].recipe_id)
    imported = await second.create_task(source="new_task", key="new", objective="Do it", recipe=view)
    started = await second.start_execution(imported.task_id, key="e")
    assert started.request["method"]["recipe"]["title"] == "Shared"
    assert started.request["method"]["recipe_id"] == shared.recipe_id
    done = await complete_execution(second, started, key="o")
    job = await generate(second, imported, done.revision_id)
    assert (job.recipe_id, job.version, job.publish_state) == (shared.recipe_id, 2, "published")
    assert model.calls[0]["context"]["base_recipe"]["recipe"]["title"] == "Shared"
    assert service.uploads[-1]["base_version"] == 1 and service.chains[shared.recipe_id]["current"] == 2


async def test_an_imported_version_continues_from_the_server_version(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("Next"))
    for version in (1, 2, 3):
        server.add("team-recipe", {**PLAIN, "title": f"V{version}"}, version=version)
    task = await rsi.create_task(source="new_task", key="k", objective="A", recipe=await rsi.get_recipe("team-recipe"))
    started = await rsi.start_execution(task.task_id, key="e")
    assert started.request["method"]["version"] == 3
    done = await complete_execution(rsi, started, key="o")
    job = await generate(rsi, task, done.revision_id)
    assert (job.recipe_id, job.version, job.publish_state) == ("team-recipe", 4, "published")
    assert (server.uploads[-1]["version"], server.uploads[-1]["base_version"]) == (4, 3)
    assert [v.version for v in (await rsi.get_recipe_versions("team-recipe"))["versions"]] == [3, 4]


async def test_a_generated_recipe_starts_a_new_chain_without_evidence(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("Next"))
    server.add("task-recipe", RECIPE, version=2, origin="generated")
    view = await rsi.get_recipe("task-recipe")
    task = await rsi.create_task(source="new_task", key="k", objective="A", recipe=view)
    started = await rsi.start_execution(task.task_id, key="e")
    method = started.request["method"]
    assert method["recipe_id"] != "task-recipe" and method["version"] == 1
    assert method["recipe"] == upload_form(RECIPE)
    done = await complete_execution(rsi, started, key="o")
    job = await generate(rsi, task, done.revision_id)
    assert (job.recipe_id, job.version, job.publish_state) == (method["recipe_id"], 2, "published")
    assert server.uploads == [{"recipe_id": job.recipe_id, "version": 2, "recipe": server.uploads[0]["recipe"]}]


@pytest.mark.parametrize("item", [
    {"recipe": {}},
    {"recipe_id": "r", "origin": "copied", "version": 1, "recipe": PLAIN},
    {"recipe_id": "r", "origin": "uploaded", "version": 0, "recipe": PLAIN},
    {"recipe_id": "r/1", "origin": "uploaded", "version": 1, "recipe": PLAIN},
    {"recipe_id": "r", "origin": "uploaded", "version": 1, "recipe": RECIPE},
    {"recipe_id": "r", "origin": "generated", "version": 1, "recipe": {**PLAIN, "steps": []}},
])
async def test_import_rejects_anything_but_a_valid_recipe_view(tmp_path, item):
    rsi, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="get_recipe|upload schema"):
        await rsi.create_task(source="new_task", key="k", objective="A", recipe=item)
    assert (await rsi.list_tasks())["items"] == []


async def test_an_uploaded_chain_already_on_this_machine_is_not_imported_again(tmp_path):
    rsi, _, _ = setup(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done.revision_id)
    with pytest.raises(ValueError, match="recipe_id"):
        await rsi.create_task(source="new_task", key="k", objective="A", recipe=await rsi.get_recipe(job.recipe_id))


async def test_recall_memory_freezes_a_snapshot_and_reaches_the_recipe_context(tmp_path):
    rsi, model, server = setup(tmp_path, recipe_reply("V1"))
    server.memory["fact"] = "a useful fact"
    task, started, done = await first_revision(rsi, recall_memory=True)
    assert started.request["memory"]["items"][0]["id"] == "fact"
    await generate(rsi, task, done.revision_id)
    assert model.calls[0]["context"]["context"]["memory"]["status"] == "captured"


async def test_recall_failure_is_raised_not_skipped(tmp_path):
    rsi, _, server = setup(tmp_path)
    server.fail_status = 503
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    with pytest.raises(RSIHTTPError):
        await rsi.start_execution(task.task_id, key="e", recall_memory=True)


async def test_recipes_published_to_memory_by_0_6_are_not_frozen_as_evidence(tmp_path):
    rsi, _, server = setup(tmp_path)
    server.memory["old"] = canonical_json({"schema": "cosens-recipe/v1", "recipe_id": "r", "version": 1, "recipe": PLAIN})
    server.memory["fact"] = "a useful fact"
    task = await rsi.create_task(source="new_task", key="k", objective="A")
    started = await rsi.start_execution(task.task_id, key="e", recall_memory=True)
    assert [item["id"] for item in started.request["memory"]["items"]] == ["fact"]


async def test_only_memory_and_recipe_routes_are_called(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi, recall_memory=True)
    job = await generate(rsi, task, done.revision_id)
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    await rsi.record_feedback(again.revision_id, feedback="fine", source="human", key="f")
    await rsi.get_task_recipe(task.task_id)
    await rsi.search_recipes("V1")
    await rsi.list_recipes()
    await rsi.get_recipe(job.recipe_id, version=1)
    assert server.requests and all(path.startswith(("/api/v1/memory/", "/api/v1/recipes")) for path in server.requests)


async def test_recipe_store_methods_need_a_client(tmp_path):
    rsi, _ = make_rsi(tmp_path)
    for call in (rsi.search_recipes("x"), rsi.list_recipes(), rsi.get_recipe("r"), rsi.publish_recipe("r")):
        with pytest.raises(ValueError, match="client"):
            await call


async def test_memory_is_a_deprecated_name_for_client(tmp_path):
    client = CoSensClient(api_key="k", _http_transport=httpx.MockTransport(FakeService()))
    with pytest.warns(DeprecationWarning, match="client="):
        rsi = LocalRSI(memory=client, model=FakeModel(), state_path=tmp_path / "s.sqlite3")
    assert rsi._atoms is client.atoms
    with pytest.raises(TypeError):
        LocalRSI(client=client, memory=client, model=FakeModel(), state_path=tmp_path / "s.sqlite3")


async def test_a_0_6_0_database_is_upgraded_in_place(tmp_path):
    assert "remote_version INTEGER" in SCHEMA
    context = {"schema": "workflow-recipe-context-v2", "task_id": "task_1", "history": [],
               "memory": {"status": "not_requested", "items": []}, "initial": True}
    db = sqlite3.connect(tmp_path / "old.sqlite3")
    db.executescript(SCHEMA.replace("remote_version INTEGER", "memory_source_id TEXT, memory_revision INTEGER NOT NULL DEFAULT 0"))
    db.execute("INSERT INTO tasks VALUES('task_1','execute',?,1)", (canonical_json({"objective": "A", "artifacts": []}),))
    db.execute("INSERT INTO recipe_candidates(id,task_id,state,phase,context_json,context_sha,recipe_json,recipe_sha,"
               "publish_state) VALUES('c1','task_1','candidate','completed',?,?,?,?,'published')",
               (canonical_json(context), fingerprint(context), canonical_json(RECIPE), fingerprint(RECIPE)))
    db.execute("INSERT INTO recipe_heads VALUES('c1','c1','c1','src1',2)")
    db.execute("INSERT INTO recipe_versions VALUES('c1','c1',3,NULL)")
    db.execute("INSERT INTO task_recipes VALUES('task_1','c1')")
    db.commit()
    db.close()

    rsi, _, server = setup(tmp_path, name="old.sqlite3")
    assert rsi._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] is None
    assert (await rsi.get_recipe_candidate("c1")).publish_state == "pending"  # it was in Memory, not the Recipe store
    assert (await rsi.publish_recipe("c1")).publish_state == "published"
    assert server.uploads == [{"recipe_id": "c1", "version": 3, "recipe": upload_form(RECIPE)}]
    await rsi.aclose()
    reopened, _, _ = setup(tmp_path, name="old.sqlite3", service=server)
    assert (await reopened.get_recipe_candidate("c1")).publish_state == "published"
    assert reopened._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 3
