import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError
from cosens.local import LocalRSI
from cosens.local.recipes import RSICandidateFailed
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
    """Memory recall and the /recipes routes, with the Recipe store's version rules."""

    def __init__(self):
        self.requests, self.uploads, self.memory, self.chains = [], [], {}, {}
        self.fail_status, self.lose_response, self.fail_reads = None, False, False

    def add(self, recipe_id, recipe, *, version=1, origin="uploaded"):
        chain = self.chains.setdefault(recipe_id, {"origin": origin, "versions": {}, "current": None})
        chain["versions"][version] = {"recipe": recipe, "sha": fingerprint(recipe), "previous_version": chain["current"]}
        chain["current"] = version

    def view(self, recipe_id, version):
        chain = self.chains[recipe_id]
        stored = chain["versions"][version]
        return {"recipe_id": recipe_id, "origin": chain["origin"], "version": version, "previous_version": stored["previous_version"],
                "current_version": chain["current"], "recipe": stored["recipe"], "recipe_sha256": stored["sha"],
                "task_id": "task_server" if chain["origin"] == "generated" else None, "created_at": "2026-11-13T00:00:00Z"}

    def rollback(self, recipe_id, version):
        """PUT /recipes/{id}/current: the stored versions stay, only the current one moves."""
        self.chains[recipe_id]["current"] = version

    def versions(self, recipe_id):
        chain = self.chains[recipe_id]
        return {"recipe_id": recipe_id, "origin": chain["origin"], "current_version": chain["current"],
                "versions": [{"version": version, "previous_version": stored["previous_version"],
                              "current": version == chain["current"], "recipe_sha256": stored["sha"]}
                             for version, stored in sorted(chain["versions"].items())]}

    def summary(self, recipe_id):
        chain = self.chains[recipe_id]
        return {"recipe_id": recipe_id, "origin": chain["origin"], "version": chain["current"],
                "title": chain["versions"][chain["current"]]["recipe"]["title"], "task_id": None, "updated_at": "t"}

    def upload(self, recipe_id, body):
        self.uploads.append({"recipe_id": recipe_id, **body})
        version, base, sha = body["version"], body.get("previous_version"), fingerprint(body["recipe"])
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
        if path == "/api/recipes" and "q" in query:
            words = query["q"].lower().split()
            found = [{**self.summary(k), "score": 0.5} for k in self.chains
                     if any(w in self.summary(k)["title"].lower() for w in words)]
            return httpx.Response(200, json={"items": found[:int(query["limit"])]})
        if path == "/api/recipes":
            items = [self.summary(k) for k in reversed(self.chains) if query.get("origin") in (None, self.chains[k]["origin"])]
            return httpx.Response(200, json={"items": items[:int(query["limit"])], "has_more": False, "next_cursor": None})
        recipe_id = path.removeprefix("/api/recipes/")
        if request.method == "GET" and self.fail_reads:
            return httpx.Response(503, json={"detail": "unavailable"})
        if request.method == "GET" and recipe_id.endswith("/versions"):
            return httpx.Response(200, json=self.versions(recipe_id.removesuffix("/versions")))
        if request.method == "PUT":
            recipe_id, version = recipe_id.split("/versions/")
            response = self.upload(recipe_id, {**body, "version": int(version)})
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
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
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
    assert [(u["recipe_id"], u["version"], u.get("previous_version")) for u in server.uploads] == [
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
    assert server.uploads[-1]["previous_version"] == 1


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
    assert [(u["version"], u.get("previous_version")) for u in server.uploads[-2:]] == [(3, 1), (3, 2)]
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
    imported = await second.create_task(key="new", objective="Do it", recipe=view)
    started = await second.start_execution(imported.task_id, source="new_task", objective="Do the task", key="e")
    assert started.request["method"]["recipe"]["title"] == "Shared"
    assert started.request["method"]["recipe_id"] == shared.recipe_id
    done = await complete_execution(second, started, key="o")
    job = await generate(second, imported, done.revision_id)
    assert (job.recipe_id, job.version, job.publish_state) == (shared.recipe_id, 2, "published")
    assert model.calls[0]["context"]["base_recipe"]["recipe"]["title"] == "Shared"
    assert service.uploads[-1]["previous_version"] == 1 and service.chains[shared.recipe_id]["current"] == 2


async def test_an_imported_version_continues_from_the_server_version(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("Next"))
    for version in (1, 2, 3):
        server.add("team-recipe", {**PLAIN, "title": f"V{version}"}, version=version)
    task = await rsi.create_task(key="k", objective="A", recipe=await rsi.get_recipe("team-recipe"))
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="e")
    assert started.request["method"]["version"] == 3
    done = await complete_execution(rsi, started, key="o")
    job = await generate(rsi, task, done.revision_id)
    assert (job.recipe_id, job.version, job.publish_state) == ("team-recipe", 4, "published")
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (4, 3)
    assert [v.version for v in (await rsi.get_recipe_versions("team-recipe"))["versions"]] == [3, 4]


async def test_a_generated_recipe_starts_a_new_chain_without_evidence(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("Next"))
    server.add("task-recipe", RECIPE, version=2, origin="generated")
    view = await rsi.get_recipe("task-recipe")
    task = await rsi.create_task(key="k", objective="A", recipe=view)
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="e")
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
        await rsi.create_task(key="k", objective="A", recipe=item)
    assert (await rsi.list_tasks())["items"] == []


async def test_an_uploaded_chain_already_on_this_machine_is_not_imported_again(tmp_path):
    rsi, _, _ = setup(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done.revision_id)
    with pytest.raises(ValueError, match="recipe_id"):
        await rsi.create_task(key="k", objective="A", recipe=await rsi.get_recipe(job.recipe_id))


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
    task = await rsi.create_task(key="k", objective="A")
    with pytest.raises(RSIHTTPError):
        await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="e", recall_memory=True)


async def test_recipes_published_to_memory_by_0_6_are_not_frozen_as_evidence(tmp_path):
    rsi, _, server = setup(tmp_path)
    server.memory["old"] = canonical_json({"schema": "cosens-recipe/v1", "recipe_id": "r", "version": 1, "recipe": PLAIN})
    server.memory["fact"] = "a useful fact"
    task = await rsi.create_task(key="k", objective="A")
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="e", recall_memory=True)
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
    assert server.requests and all(path.startswith(("/api/v1/memory/", "/api/recipes")) for path in server.requests)


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
    old_schema = SCHEMA.replace("remote_version INTEGER,\n  remote_max_version INTEGER",
                                "memory_source_id TEXT, memory_revision INTEGER NOT NULL DEFAULT 0")
    db.executescript(old_schema.replace("tasks(id TEXT PRIMARY KEY, context_json", "tasks(id TEXT PRIMARY KEY, mode TEXT NOT NULL, context_json"))
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
    task = await rsi.get_task("task_1")
    assert (task.objective, task.artifacts, [r["recipe_id"] for r in task.recipes]) == ("A", [], ["c1"])
    assert (await rsi.get_recipe_candidate("c1")).publish_state == "pending"  # it was in Memory, not the Recipe store
    assert (await rsi.publish_recipe("c1")).publish_state == "published"
    assert server.uploads == [{"recipe_id": "c1", "version": 3, "recipe": upload_form(RECIPE)}]
    await rsi.aclose()
    reopened, _, _ = setup(tmp_path, name="old.sqlite3", service=server)
    assert (await reopened.get_recipe_candidate("c1")).publish_state == "published"
    assert reopened._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 3


async def three_versions(rsi, *, key="t1"):
    """A chain of V1..V3, each uploaded; returns the task and the three versions."""
    task, _, done = await first_revision(rsi, key=key)
    versions = [await generate(rsi, task, done.revision_id)]
    for name in ("two", "three"):
        versions.append(await improve(rsi, task, key + name))
    assert [(v.version, v.publish_state) for v in versions] == [(1, "published"), (2, "published"), (3, "published")]
    return task, versions


async def test_a_learning_after_a_rollback_builds_on_the_service_current_version(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, (_, v2, _) = await three_versions(rsi)
    server.rollback(v2.recipe_id, 2)
    job = await improve(rsi, task, "four")
    assert (job.version, job.parent_candidate_id, job.publish_state) == (4, v2.recipe_candidate_id, "published")
    assert model.calls[-1]["context"]["base_recipe"]["version"] == 2
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (4, 2)
    assert server.chains[v2.recipe_id]["current"] == 4
    flags = [(v.version, v.rolled_back) for v in (await rsi.get_recipe_versions(v2.recipe_id)).versions]
    assert flags == [(1, False), (2, False), (3, True), (4, False)]


async def test_a_rollback_found_only_on_upload_fails_the_candidate_and_a_resume_publishes_it(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("Doomed"), recipe_reply("V4"))
    task, (_, v2, v3) = await three_versions(rsi)
    server.rollback(v2.recipe_id, 2)
    server.fail_reads = True  # the check before learning cannot read the service and keeps the local base
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="four")
    done = await complete_execution(rsi, started, key="four-out")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert (candidate.version, candidate.parent_candidate_id) == (4, v3.recipe_candidate_id)
    server.fail_reads = False
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert (caught.value.code, caught.value.retryable) == ("previous_version_changed", True)
    assert sorted(server.chains[v2.recipe_id]["versions"]) == [1, 2, 3] and server.chains[v2.recipe_id]["current"] == 2
    assert (await rsi.get_task_recipe(task.task_id)).current.recipe_candidate_id == v2.recipe_candidate_id
    await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.publish_state, job.recipe["title"]) == (
        5, v2.recipe_candidate_id, "published", "V4")
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (5, 2)


async def test_a_candidate_built_on_a_rolled_back_version_is_never_uploaded(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("Doomed"))
    task, (_, v2, _) = await three_versions(rsi)
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="four")
    done = await complete_execution(rsi, started, key="four-out")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    server.rollback(v2.recipe_id, 2)  # after the check before learning
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    with pytest.raises(RSICandidateFailed):
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    published = await rsi.publish_recipe(v2.recipe_id)
    assert (published.recipe_candidate_id, published.publish_state) == (v2.recipe_candidate_id, "published")
    assert "Doomed" not in json.dumps([chain["versions"] for chain in server.chains.values()])
    assert [u["version"] for u in server.uploads] == [1, 2, 3, 4]  # the one refused attempt, never stored


async def test_after_a_rollback_versions_are_numbered_above_every_stored_version(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, (_, v2, _) = await three_versions(rsi)
    server.add(v2.recipe_id, PLAIN, version=4)  # uploaded from another machine
    server.rollback(v2.recipe_id, 2)
    job = await improve(rsi, task, "five")
    assert (job.version, job.parent_candidate_id, job.publish_state) == (5, v2.recipe_candidate_id, "published")
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (5, 2)


async def test_a_rollback_to_a_version_this_machine_never_had_imports_it(tmp_path):
    rsi, model, server = setup(tmp_path, recipe_reply("Next"))
    for version in (1, 2, 3):
        server.add("team-recipe", {**PLAIN, "title": f"V{version}"}, version=version)
    task = await rsi.create_task(key="k", objective="A", recipe=await rsi.get_recipe("team-recipe"))
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="e")
    done = await complete_execution(rsi, started, key="o")
    server.rollback("team-recipe", 2)
    job = await generate(rsi, task, done.revision_id)
    assert (job.version, job.publish_state) == (4, "published")
    assert model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "V2"
    versions = (await rsi.get_recipe_versions("team-recipe")).versions
    assert [(v.version, v.rolled_back, v.publish_state) for v in versions] == [
        (2, False, "published"), (3, True, "published"), (4, False, "published")]
    assert job.parent_candidate_id == versions[0].recipe_candidate_id
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (4, 2)


async def test_candidates_queued_behind_a_rolled_back_one_fail_and_resume_on_the_current_version(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("Doomed"),
                               recipe_reply("A"), recipe_reply("B"))
    task, (_, v2, v3) = await three_versions(rsi)
    server.rollback(v2.recipe_id, 2)
    server.fail_reads = True  # neither check before learning can read the service
    queued = []
    for key in ("four", "five"):
        started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
        done = await complete_execution(rsi, started, key=key + "-out")
        queued.append(await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id))
    a, b = queued
    assert (a.version, a.parent_candidate_id, b.version, b.parent_candidate_id) == (
        4, v3.recipe_candidate_id, 5, a.recipe_candidate_id)
    server.fail_reads = False
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", a.recipe_candidate_id)
    assert caught.value.code == "previous_version_changed"
    with pytest.raises(RSICandidateFailed) as caught:  # not left waiting for A to be resumed
        await rsi.wait_workflow_job("recipe_candidate", b.recipe_candidate_id, timeout=5)
    assert (caught.value.code, caught.value.retryable) == ("previous_version_failed", True)
    assert [u["version"] for u in server.uploads] == [1, 2, 3, 4]  # A's refused attempt; B is never uploaded
    for candidate in (a, b):
        await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    job_a = await rsi.wait_workflow_job("recipe_candidate", a.recipe_candidate_id)
    job_b = await rsi.wait_workflow_job("recipe_candidate", b.recipe_candidate_id)
    assert (job_a.version, job_a.parent_candidate_id, job_a.publish_state, job_a.recipe["title"]) == (
        6, v2.recipe_candidate_id, "published", "A")
    assert (job_b.version, job_b.parent_candidate_id, job_b.publish_state, job_b.recipe["title"]) == (
        7, a.recipe_candidate_id, "published", "B")
    assert model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "A"
    assert [(u["version"], u["previous_version"]) for u in server.uploads[4:]] == [(6, 2), (7, 6)]
    assert server.chains[v2.recipe_id]["current"] == 7


async def test_a_candidate_on_a_rolled_back_base_fails_when_the_service_moves_forward_again(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("Doomed"), recipe_reply("V4"))
    task, (v1, _, v3) = await three_versions(rsi)
    server.rollback(v1.recipe_id, 1)
    started = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key="four")
    done = await complete_execution(rsi, started, key="four-out")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)  # follows the rollback
    assert (candidate.version, candidate.parent_candidate_id) == (4, v1.recipe_candidate_id)
    server.rollback(v1.recipe_id, 3)  # forward again, after the check before learning
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert (caught.value.code, caught.value.retryable) == ("previous_version_changed", True)
    assert sorted(server.chains[v1.recipe_id]["versions"]) == [1, 2, 3] and server.chains[v1.recipe_id]["current"] == 3
    assert (await rsi.get_task_recipe(task.task_id)).current.recipe_candidate_id == v3.recipe_candidate_id
    await rsi.resume_recipe_candidate(candidate.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.publish_state, job.recipe["title"]) == (
        5, v3.recipe_candidate_id, "published", "V4")
    assert [(u["version"], u["previous_version"]) for u in server.uploads[3:]] == [(4, 1), (5, 3)]


async def test_a_learning_after_a_rollback_and_forward_again_builds_on_the_service_current_version(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 6)))
    task, (v1, _, v3) = await three_versions(rsi)
    server.rollback(v1.recipe_id, 1)
    v4 = await improve(rsi, task, "four")
    assert (v4.version, v4.parent_candidate_id, v4.publish_state) == (4, v1.recipe_candidate_id, "published")
    server.rollback(v1.recipe_id, 3)
    job = await improve(rsi, task, "five")
    assert (job.version, job.parent_candidate_id, job.publish_state) == (5, v3.recipe_candidate_id, "published")
    assert model.calls[-1]["context"]["base_recipe"]["version"] == 3
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (5, 3)
    flags = [(v.version, v.rolled_back) for v in (await rsi.get_recipe_versions(v1.recipe_id)).versions]
    assert flags == [(1, False), (2, False), (3, False), (4, True), (5, False)]


async def test_the_check_before_learning_follows_the_service_forward_again(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("OnV3"))
    task, (v1, _, v3) = await three_versions(rsi)
    server.rollback(v1.recipe_id, 1)
    learned = []
    for key in ("four", "five"):
        execution = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
        learned.append(await complete_execution(rsi, execution, key=key + "-out"))
    first = await rsi.start_recipe_candidate(task.task_id, revision_id=learned[0].revision_id)  # follows the rollback
    assert (first.version, first.parent_candidate_id) == (4, v1.recipe_candidate_id)
    server.rollback(v1.recipe_id, 3)
    second = await rsi.start_recipe_candidate(task.task_id, revision_id=learned[1].revision_id)
    assert (second.version, second.parent_candidate_id) == (5, v3.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", second.recipe_candidate_id)
    assert (job.version, job.publish_state) == (5, "published")
    assert model.calls[-1]["context"]["base_recipe"]["version"] == 3
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (5, 3)


async def test_following_a_rollback_before_learning_fails_finished_versions_never_uploaded(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("X"), recipe_reply("Y"),
                           recipe_reply("X again"))
    task, (_, v2, _) = await three_versions(rsi)
    started = []
    for key in ("x", "z", "y"):
        execution = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
        started.append(await complete_execution(rsi, execution, key=key + "-out"))
    x = await rsi.start_recipe_candidate(task.task_id, revision_id=started[0].revision_id)
    await rsi._settle(x.recipe_candidate_id)  # committed as the current version; a crash came before its upload
    assert (await rsi.get_recipe_candidate(x.recipe_candidate_id)).publish_state == "pending"
    z = await rsi.start_recipe_candidate(task.task_id, revision_id=started[1].revision_id)
    assert (z.version, z.parent_candidate_id) == (5, x.recipe_candidate_id)
    server.rollback(v2.recipe_id, 2)
    y = await rsi.start_recipe_candidate(task.task_id, revision_id=started[2].revision_id)  # follows the rollback
    assert (y.version, y.parent_candidate_id) == (6, v2.recipe_candidate_id)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", x.recipe_candidate_id)
    assert (caught.value.code, caught.value.retryable) == ("previous_version_changed", True)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.wait_workflow_job("recipe_candidate", z.recipe_candidate_id, timeout=5)
    assert caught.value.code == "previous_version_failed"
    assert (await rsi.wait_workflow_job("recipe_candidate", y.recipe_candidate_id)).publish_state == "published"
    await rsi.resume_recipe_candidate(x.recipe_candidate_id)
    job = await rsi.wait_workflow_job("recipe_candidate", x.recipe_candidate_id)
    assert (job.version, job.parent_candidate_id, job.publish_state, job.recipe["title"]) == (
        7, y.recipe_candidate_id, "published", "X again")
    assert [(u["version"], u["previous_version"]) for u in server.uploads[3:]] == [(6, 2), (7, 6)]


async def test_publish_recipe_raises_when_the_service_rolled_back_below_the_version(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, (_, v2, _) = await three_versions(rsi)
    server.fail_status = 503
    v4 = await improve(rsi, task, "four")
    server.fail_status = None
    assert (v4.current, v4.publish_state) == (True, "failed")
    server.rollback(v2.recipe_id, 2)
    with pytest.raises(RSICandidateFailed) as caught:
        await rsi.publish_recipe(v2.recipe_id)
    assert (caught.value.code, caught.value.candidate_id) == ("previous_version_changed", v4.recipe_candidate_id)
    published = await rsi.publish_recipe(v2.recipe_id)
    assert (published.recipe_candidate_id, published.publish_state) == (v2.recipe_candidate_id, "published")
    assert [u["version"] for u in server.uploads] == [1, 2, 3, 4]  # the refused retry; V4 is never stored


async def test_a_local_version_whose_number_another_machine_uploaded_is_not_followed(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, _, done = await first_revision(rsi)
    first = await generate(rsi, task, done.revision_id)
    server.fail_status = 503
    await improve(rsi, task, "two")  # our own V2 never reaches the server
    server.fail_status = None
    server.add(first.recipe_id, PLAIN, version=2)  # another machine's v2
    v3 = await improve(rsi, task, "three")
    assert v3.publish_state == "publish_conflict"
    v4 = await improve(rsi, task, "four")  # the local V2 is not the service's v2, so the check before learning keeps V3
    assert (v4.version, v4.parent_candidate_id, v4.publish_state) == (4, v3.recipe_candidate_id, "publish_conflict")
    assert model.calls[-1]["context"]["base_recipe"]["recipe"]["title"] == "V3"
    assert ((await rsi.get_recipe_candidate(v3.recipe_candidate_id)).status) == "candidate"
    assert [(u["version"], u.get("previous_version")) for u in server.uploads] == [(1, None), (3, 1), (4, 1)]
    assert sorted(server.chains[first.recipe_id]["versions"]) == [1, 2] and server.chains[first.recipe_id]["current"] == 2
    assert rsi._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 1


async def test_a_version_imported_from_another_machine_builds_on_the_local_copy_of_its_previous_version(tmp_path):
    rsi, model, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, (v1, _, v3) = await three_versions(rsi)
    server.add(v1.recipe_id, PLAIN, version=4)  # uploaded from another machine on v3
    job = await improve(rsi, task, "five")
    versions = (await rsi.get_recipe_versions(v1.recipe_id)).versions
    assert [(v.version, v.rolled_back, v.publish_state) for v in versions] == [
        (1, False, "published"), (2, False, "published"), (3, False, "published"), (4, False, "published"),
        (5, False, "published")]
    assert versions[3].parent_candidate_id == v3.recipe_candidate_id
    assert (job.parent_candidate_id, model.calls[-1]["context"]["base_recipe"]["recipe"]["title"]) == (
        versions[3].recipe_candidate_id, "Shared")
    assert (server.uploads[-1]["version"], server.uploads[-1]["previous_version"]) == (5, 4)


async def test_an_import_keeps_a_local_version_the_service_stores_though_its_upload_reply_was_lost(tmp_path):
    rsi, _, server = setup(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 5)))
    task, _, done = await first_revision(rsi)
    v1 = await generate(rsi, task, done.revision_id)
    await improve(rsi, task, "two")
    server.lose_response = True  # V3 is stored on the server, but the reply never arrives
    v3 = await improve(rsi, task, "three")
    server.lose_response = False
    assert v3.publish_state == "failed" and server.chains[v1.recipe_id]["current"] == 3
    server.add(v1.recipe_id, PLAIN, version=4)  # uploaded from another machine on v3
    job = await improve(rsi, task, "five")
    assert (job.version, job.publish_state) == (5, "published")
    versions = (await rsi.get_recipe_versions(v1.recipe_id)).versions
    assert [(v.version, v.status, v.rolled_back) for v in versions] == [
        (1, "candidate", False), (2, "candidate", False), (3, "candidate", False), (4, "candidate", False),
        (5, "candidate", False)]
    assert versions[3].parent_candidate_id == v3.recipe_candidate_id


async def test_an_upload_reply_after_another_learning_followed_a_rollback_changes_nothing(tmp_path):
    server = FakeService()
    gate = {"armed": False, "stored": asyncio.Event(), "go": asyncio.Event()}

    async def handler(request):
        response = server(request)
        if gate["armed"] and request.method == "PUT":
            gate["armed"] = False
            server.rollback(request.url.path.removeprefix("/api/recipes/").split("/versions/")[0], 2)
            gate["stored"].set()  # X is stored as v4 and the user rolled back to 2 before X's reply arrives
            await gate["go"].wait()
        return response

    client = CoSensClient(api_key="k", _http_transport=httpx.MockTransport(handler))
    rsi, _ = make_rsi(tmp_path, *(recipe_reply(f"V{n}") for n in range(1, 4)), recipe_reply("X"), recipe_reply("Y"),
                      client=client, name="s.sqlite3")
    task, (_, v2, _) = await three_versions(rsi)
    learned = []
    for key in ("x", "y"):
        execution = await rsi.start_execution(task.task_id, source="new_task", objective="Do the task", key=key)
        learned.append(await complete_execution(rsi, execution, key=key + "-out"))
    x = await rsi.start_recipe_candidate(task.task_id, revision_id=learned[0].revision_id)
    gate["armed"] = True
    waiting = asyncio.create_task(rsi.wait_workflow_job("recipe_candidate", x.recipe_candidate_id))
    await gate["stored"].wait()
    y = await rsi.start_recipe_candidate(task.task_id, revision_id=learned[1].revision_id)  # follows the rollback
    assert (y.version, y.parent_candidate_id) == (5, v2.recipe_candidate_id)
    gate["go"].set()
    with pytest.raises(RSICandidateFailed) as caught:
        await waiting
    assert caught.value.code == "previous_version_changed"
    stored = await rsi.get_recipe_candidate(x.recipe_candidate_id)
    assert (stored.status, stored.publish_state) == ("failed", None)
    assert rsi._store.read_one("SELECT remote_version FROM recipe_heads")["remote_version"] == 2
    assert (await rsi.get_task_recipe(task.task_id)).current.recipe_candidate_id == v2.recipe_candidate_id
    assert (await rsi.wait_workflow_job("recipe_candidate", y.recipe_candidate_id)).publish_state == "published"
    assert [(u["version"], u.get("previous_version")) for u in server.uploads] == [
        (1, None), (2, 1), (3, 2), (4, 3), (5, 2)]
