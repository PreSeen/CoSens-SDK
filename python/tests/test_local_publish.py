import json

import httpx
import pytest

from cosens import CoSensClient
from cosens.errors import RSIHTTPError
from cosens.local.publish import parse_publication, publication_text
from local_rsi_helpers import complete_execution, first_revision, make_rsi, recipe_reply

RECIPE = {"title": "T", "when": "W", "when_not": "N",
          "steps": [{"id": "s1", "instruction": "I", "evidence_refs": [{"artifact_id": "a"}]}],
          "lessons": [{"when": "W", "do": "D", "why": "Y", "when_not": "N", "evidence_refs": [{"feedback_id": "f"}]}]}


class FakeMemory:
    """The /v1/memory routes with Memory's compare-and-write behavior."""

    def __init__(self):
        self.requests, self.sources, self.fail_status = [], {}, None

    def __call__(self, request):
        path = request.url.path
        self.requests.append(path)
        body = json.loads(request.content or b"{}")
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"detail": "no"})
        if path == "/api/v1/memory/ingest":
            source_id = body.get("source_id") or f"src{len(self.sources) + 1}"
            current = self.sources.get(source_id, {"revision": 0})["revision"]
            if body.get("expected_revision", 0) != current:
                return httpx.Response(409, json={"detail": "revision changed"})
            self.sources[source_id] = {"revision": current + 1, "text": body["content"]}
            return httpx.Response(200, json={"source_id": source_id, "revision": current + 1, "job_id": "j", "state": "pending"})
        if path == "/api/v1/memory/recall":
            items = [{"id": k, "text": v["text"], "revision": v["revision"], "score": 0.9, "kind": "source",
                      "refs": [{"source_id": k, "revision": v["revision"], "content_hash": "h", "start": 0, "end": 1}]}
                     for k, v in self.sources.items()]
            return httpx.Response(200, json={"items": items + [{"id": "plain", "text": "ordinary memory", "revision": 1, "score": 0.5}]})
        return httpx.Response(500, json={"detail": "unexpected route " + path})


def test_publication_drops_evidence_and_round_trips():
    text = publication_text("r1", 2, RECIPE)
    assert "evidence_refs" not in text
    parsed = parse_publication(text)
    assert parsed["version"] == 2 and parsed["recipe"]["steps"] == [{"id": "s1", "instruction": "I"}]


@pytest.mark.parametrize("text", ["plain text", '{"schema": "other"}', "[]", None])
def test_other_memory_text_is_not_a_publication(text):
    assert parse_publication(text) is None


def setup(tmp_path, *replies, name="s.sqlite3", memory_server=None):
    server = memory_server or FakeMemory()
    client = CoSensClient(api_key="k", _http_transport=httpx.MockTransport(server))
    rsi, model = make_rsi(tmp_path, *replies, memory=client, name=name)
    return rsi, model, server


async def generate(rsi, task, revision_id):
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=revision_id)
    return await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)


async def test_new_versions_update_the_same_memory_source(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    assert (await generate(rsi, task, done.revision_id)).publish_state == "published"
    started = await rsi.start_execution(task.task_id, key="two")
    second = await complete_execution(rsi, started, key="two-out")
    assert (await generate(rsi, task, second.revision_id)).publish_state == "published"
    assert list(server.sources) == ["src1"] and server.sources["src1"]["revision"] == 2
    assert json.loads(server.sources["src1"]["text"])["recipe"]["title"] == "V2"


async def test_publish_failure_keeps_the_local_version_and_can_retry(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"))
    server.fail_status = 500
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done.revision_id)
    assert job.status == "candidate" and job.current and job.publish_state == "failed"
    server.fail_status = None
    assert (await rsi.publish_recipe(job.recipe_id)).publish_state == "published"
    assert (await rsi.publish_recipe(job.recipe_id)).publish_state == "published"
    assert server.sources["src1"]["revision"] == 1


async def test_another_machine_publishing_first_is_a_conflict(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done.revision_id)
    server.sources["src1"]["revision"] = 5
    started = await rsi.start_execution(task.task_id, key="two")
    second = await complete_execution(rsi, started, key="two-out")
    job = await generate(rsi, task, second.revision_id)
    assert job.current and job.publish_state == "publish_conflict"
    assert server.sources["src1"]["revision"] == 5


async def test_recall_import_and_republish_from_another_machine(tmp_path):
    server = FakeMemory()
    first, _, _ = setup(tmp_path, recipe_reply("Shared"), name="a.sqlite3", memory_server=server)
    task, _, done = await first_revision(first)
    await generate(first, task, done.revision_id)

    second, model, _ = setup(tmp_path, recipe_reply("Improved"), name="b.sqlite3", memory_server=server)
    found = await second.recall_recipes("how to do the task")
    assert [item.source_id for item in found] == ["src1"] and found[0].recipe.title == "Shared"
    imported = await second.create_task(source="new_task", key="new", objective="Do it", recipe=found[0])
    started = await second.start_execution(imported.task_id, key="e")
    assert started.request["method"]["recipe"]["title"] == "Shared"
    assert "evidence_refs" not in started.request["method"]["recipe"]["steps"][0]
    done = await complete_execution(second, started, key="o")
    job = await generate(second, imported, done.revision_id)
    assert job.version == 2 and job.publish_state == "published"
    assert model.calls[0]["context"]["base_recipe"]["recipe"]["title"] == "Shared"
    assert server.sources["src1"]["revision"] == 2


async def test_recall_memory_freezes_a_snapshot_and_reaches_the_recipe_context(tmp_path):
    rsi, model, server = setup(tmp_path, recipe_reply("V1"))
    server.sources["fact"] = {"revision": 1, "text": "a useful fact"}
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


async def test_published_recipes_are_not_frozen_as_memory_evidence(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    await generate(rsi, task, done.revision_id)
    server.sources["fact"] = {"revision": 1, "text": "a useful fact"}
    other = await rsi.create_task(source="new_task", key="other", objective="Other")
    started = await rsi.start_execution(other.task_id, key="e", recall_memory=True)
    assert [item["id"] for item in started.request["memory"]["items"]] == ["fact"]


async def test_a_publish_lost_to_a_crash_is_recovered_on_the_next_wait(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"))
    task, _, done = await first_revision(rsi)
    job = await generate(rsi, task, done.revision_id)
    server.sources.clear()
    with rsi._store.transaction() as db:
        db.execute("UPDATE recipe_candidates SET publish_state='pending'")
        db.execute("UPDATE recipe_heads SET memory_source_id=NULL,memory_revision=0")
    assert (await rsi.wait_workflow_job("recipe_candidate", job.recipe_candidate_id)).publish_state == "published"
    assert server.sources["src1"]["revision"] == 1


async def test_import_continues_the_published_version_number(tmp_path):
    rsi, _, _ = setup(tmp_path, recipe_reply("Next"))
    item = {"source_id": "s", "revision": 2, "version": 3, "score": 0.9,
            "recipe": {"title": "T", "when": "W", "when_not": "N", "steps": [{"id": "s1", "instruction": "I"}], "lessons": []}}
    task = await rsi.create_task(source="new_task", key="k", objective="A", recipe=item)
    started = await rsi.start_execution(task.task_id, key="e")
    assert started.request["method"]["version"] == 3
    done = await complete_execution(rsi, started, key="o")
    assert (await generate(rsi, task, done.revision_id)).version == 4


async def test_only_memory_routes_are_called(tmp_path):
    rsi, _, server = setup(tmp_path, recipe_reply("V1"), recipe_reply("V2"))
    task, _, done = await first_revision(rsi, recall_memory=True)
    await generate(rsi, task, done.revision_id)
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    await rsi.record_feedback(again.revision_id, feedback="fine", source="human", key="f")
    await rsi.get_task_recipe(task.task_id)
    assert server.requests and all(path.startswith("/api/v1/memory/") for path in server.requests)


async def test_import_rejects_an_invalid_item(tmp_path):
    rsi, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="recall_recipes"):
        await rsi.create_task(source="new_task", key="k", objective="A", recipe={"recipe": {}})
