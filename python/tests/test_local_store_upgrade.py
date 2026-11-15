"""Opening a LocalRSI store written by an earlier SDK upgrades it once: evidence references name executions (version 1),
then tasks become capabilities and each execution carries its own input (version 2)."""
import hashlib
import json
import sqlite3

import pytest

from cosens.local import LocalStoreUpgradeError
from cosens.local.store import STORE_VERSION, LocalStore, canonical_json, fingerprint
from local_rsi_helpers import complete_execution, make_rsi, recipe_reply

COLUMNS = "id,context_json,context_sha,recipe_json,recipe_sha,attribution_json"


def attribution_citing_an_event(payload):
    history = payload["context"]["history"]
    event = {"execution_id": history[-1]["execution_id"], "event_id": history[-1]["events"][0]["id"]}
    feedback = {"feedback_id": history[0]["feedback"][0]["feedback_id"]}
    return {"actionable": True, "verified_success": False, "new_evidence": [], "limitations": "",
            "possible_causes": [{"reason": "r", "uncertainty": "u", "evidence_refs": [event, feedback]}]}


async def learned_store(tmp_path):
    """V1 learned from an execution of a brief, then V2 from its correction after feedback; V2's Recipe and attribution
    cite events."""
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_citing_an_event, recipe_reply("V2"))
    task = await rsi.create_task(key="t1", objective="Do the task")
    brief = await rsi.artifact(name="brief.md", content="The brief")
    started = await rsi.start_execution(task.task_id, key="t1-e", source="new_task", objective="Do the task",
                                        artifacts=[brief.artifact_id])
    done = await complete_execution(rsi, started, key="t1-out")
    first = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert (await rsi.wait_workflow_job("recipe_candidate", first.recipe_candidate_id)).status == "candidate"
    await rsi.record_feedback(done.revision_id, feedback="wrong", source="human", key="f")
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=again.revision_id)
    learned = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert learned.status == "candidate"
    await rsi.aclose()
    path = tmp_path / "state.sqlite3"
    add_imported_candidate(path, task.task_id)
    return path, learned.recipe_candidate_id


def add_imported_candidate(path, task_id):
    """A version imported from the Recipe store: empty history, no source execution, no evidence references."""
    context = {"schema": "workflow-recipe-context-v2", "task_id": task_id, "history": [], "initial": True,
               "memory": {"status": "not_requested", "items": []},
               "imported_from": {"recipe_id": "team_recipe", "origin": "uploaded", "version": 3}}
    recipe = {"title": "T", "when": "W", "when_not": "N", "steps": [{"id": "s1", "instruction": "I"}], "lessons": []}
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO recipe_candidates(id,task_id,revision_id,state,phase,context_json,context_sha,recipe_json,recipe_sha) "
                   "VALUES('recipe_candidate_imported',?,NULL,'candidate','completed',?,?,?,?)",
                   (task_id, canonical_json(context), fingerprint(context), canonical_json(recipe), fingerprint(recipe)))


def rows(path):
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT {COLUMNS} FROM recipe_candidates ORDER BY rowid").fetchall()


def user_version(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


TABLES = ("tasks", "executions", "task_recipes", "recipe_candidates", "idempotency")


def state(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid").fetchall() for table in TABLES}


def columns(path, table):
    with sqlite3.connect(path) as db:
        return [(row[1], row[2], row[3], row[5]) for row in db.execute(f"PRAGMA table_info({table})")]


def old_request(request):
    """A request as version 1 froze it: the task's context under "context", its version beside it."""
    request = dict(request)
    request["context"], version = request.pop("input"), request.pop("task_context")["context_version"]
    request["context_version"] = version
    return request


def old_context(context):
    """A learning context as version 1 froze it: history entries name the input "context", no task context."""
    context = {key: value for key, value in context.items() if key not in ("task_context", "task_materials")}
    context["schema"] = "workflow-recipe-context-v2"
    context["history"] = [{("context" if key == "input" else key): value for key, value in entry.items()}
                          for entry in context["history"]]
    return context


def learning_receipts_report_their_context(db):
    """A learning's start receipt reports the hash of the context as it was frozen then."""
    for key, response_text in db.execute(
            "SELECT request_key,response_json FROM idempotency WHERE operation='recipe_candidate'").fetchall():
        response = json.loads(response_text)
        (context_text,) = db.execute("SELECT context_json FROM recipe_candidates WHERE id=?",
                                     (response["recipe_candidate_id"],)).fetchone()
        response["context_sha256"] = fingerprint(json.loads(context_text))
        db.execute("UPDATE idempotency SET response_json=? WHERE operation='recipe_candidate' AND request_key=?",
                   (canonical_json(response), key))


def receipts_kept_by_the_upgrade(current, old):
    """The idempotency rows an upgrade leaves: create_task receipts as they were, and every create_task and
    start_execution receipt with the hash of its old request; the rest as the current SDK writes them."""
    before = {(row[1], row[2]): row for row in old["idempotency"]}
    kept = []
    for row in current["idempotency"]:
        old_row = before[(row[1], row[2])]
        if row[1] == "create_task":
            row = old_row
        elif row[1].startswith("start_execution:"):
            row = (*row[:3], old_row[3], row[4])
        kept.append(row)
    return {**current, "idempotency": kept}


def to_version_1(path, *, break_context_hash_of=None):
    """Write the store back as the previous SDK did: a task holds a mode and the instance input, requests freeze it as
    "context", executions have no instance, start receipts no kind and the hash of that request, create_task receipts
    the old task request and response, a Recipe may serve several tasks, learning contexts are v2."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE tasks_v1(
          id TEXT PRIMARY KEY, mode TEXT NOT NULL, context_json TEXT NOT NULL, context_version INTEGER NOT NULL);
        INSERT INTO tasks_v1(rowid,id,mode,context_json,context_version)
          SELECT rowid,id,'execute',json_object('objective',json_extract(context_json,'$.objective'),
                                                'self_contained',json('false'),'artifacts',json('[]')),context_version
          FROM tasks;
        CREATE TABLE executions_v1(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, status TEXT NOT NULL,
          request_json TEXT NOT NULL, request_sha TEXT NOT NULL, result_json TEXT);
        INSERT INTO executions_v1(rowid,id,task_id,kind,status,request_json,request_sha,result_json)
          SELECT rowid,id,task_id,kind,status,request_json,request_sha,result_json FROM executions;
        CREATE TABLE task_recipes_v1(
          task_id TEXT PRIMARY KEY REFERENCES tasks(id), recipe_id TEXT NOT NULL REFERENCES recipe_heads(recipe_id));
        INSERT INTO task_recipes_v1(rowid,task_id,recipe_id) SELECT rowid,task_id,recipe_id FROM task_recipes;
        DROP TABLE tasks; DROP TABLE executions; DROP TABLE task_recipes;
        ALTER TABLE tasks_v1 RENAME TO tasks; ALTER TABLE executions_v1 RENAME TO executions;
        ALTER TABLE task_recipes_v1 RENAME TO task_recipes;
    """)
    for execution_id, request_text in db.execute("SELECT id,request_json FROM executions").fetchall():
        db.execute("UPDATE executions SET request_json=? WHERE id=?",
                   (canonical_json(old_request(json.loads(request_text))), execution_id))
    # The task held the instance input that its first execution froze as "context".
    for task_id, in db.execute("SELECT id FROM tasks").fetchall():
        first = db.execute("SELECT request_json FROM executions WHERE task_id=? ORDER BY rowid", (task_id,)).fetchone()
        if first is not None:
            db.execute("UPDATE tasks SET context_json=? WHERE id=?",
                       (canonical_json(json.loads(first[0])["context"]), task_id))
    for operation, key, response_text in db.execute(
            "SELECT operation,request_key,response_json FROM idempotency "
            "WHERE operation LIKE 'start_execution:%' OR operation LIKE 'start_revision:%'").fetchall():
        response = json.loads(response_text)
        del response["instance_id"], response["kind"]
        response["request"] = old_request(response["request"])
        db.execute("UPDATE idempotency SET response_json=? WHERE operation=? AND request_key=?",
                   (canonical_json(response), operation, key))
        if operation.startswith("start_execution:"):
            db.execute("UPDATE idempotency SET request_hash=? WHERE operation=? AND request_key=?",
                       (fingerprint(response["request"]), operation, key))
    for key, response_text in db.execute(
            "SELECT request_key,response_json FROM idempotency WHERE operation='create_task'").fetchall():
        task_id = json.loads(response_text)["task_id"]
        (context_text,) = db.execute("SELECT context_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        old_task = {"source": "new_task", **json.loads(context_text)}
        db.execute("UPDATE idempotency SET request_hash=?,response_json=? WHERE operation='create_task' AND request_key=?",
                   (fingerprint(old_task), canonical_json({"task_id": task_id, "source": "new_task", "status": "ready",
                                                           "missing": [], "context_version": 1}), key))
    for row_id, context_text in db.execute(
            "SELECT id,context_json FROM recipe_candidates WHERE revision_id IS NOT NULL").fetchall():
        context = old_context(json.loads(context_text))
        sha = "0" * 64 if break_context_hash_of == row_id else fingerprint(context)
        db.execute("UPDATE recipe_candidates SET context_json=?,context_sha=? WHERE id=?", (canonical_json(context), sha, row_id))
    learning_receipts_report_their_context(db)
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()


def to_revisions(path, *, break_hash_of=None, unknown_execution=False):
    """Write the store back as an SDK before version 1 did: revision keys, history origin "revision", hashes of that text."""
    to_version_1(path)
    db = sqlite3.connect(path)
    revision_of = dict(db.execute("SELECT execution_id,id FROM revisions"))
    if unknown_execution:
        revision_of = {key: "revision_missing" for key in revision_of}

    def refs(value):
        if isinstance(value, list):
            return [refs(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "execution_id" in value and "event_id" in value:
            value = {**value, "revision_id": revision_of[value["execution_id"]]}
            del value["execution_id"]
        return {key: refs(item) for key, item in value.items()}

    def entry(item):
        item = {**item, "revision_id": revision_of[item["execution_id"]],
                "origin": "revision" if item["origin"] == "correction" else item["origin"],
                "feedback": [{**{k: v for k, v in f.items() if k != "execution_id"}, "revision_id": revision_of[f["execution_id"]]}
                             for f in item["feedback"]]}
        del item["execution_id"]
        return item

    for row_id, context_text, _, recipe_text, _, attribution_text in db.execute(f"SELECT {COLUMNS} FROM recipe_candidates").fetchall():
        context = json.loads(context_text)
        if "source_execution_id" in context:
            context["source_revision_id"] = revision_of[context.pop("source_execution_id")]
        context["history"] = [entry(item) for item in context["history"]]
        recipe = refs(json.loads(recipe_text)) if recipe_text else None
        attribution = refs(json.loads(attribution_text)) if attribution_text else None
        recipe_sha = fingerprint(recipe) if recipe is not None else None
        if break_hash_of == row_id:
            recipe_sha = "0" * 64
        db.execute("UPDATE recipe_candidates SET context_json=?,context_sha=?,recipe_json=?,recipe_sha=?,attribution_json=? WHERE id=?",
                   (canonical_json(context), fingerprint(context), canonical_json(recipe) if recipe is not None else None,
                    recipe_sha, canonical_json(attribution) if attribution is not None else None, row_id))
    learning_receipts_report_their_context(db)
    db.execute("PRAGMA user_version=0")
    db.commit()
    db.close()


async def test_local_store_saves_recipe_and_context_as_their_fingerprint_text(tmp_path):
    """The upgrade's hash check relies on this: stored text is canonical_json, and its SHA-256 is the stored hash."""
    path, _ = await learned_store(tmp_path)
    for _, context_text, context_sha, recipe_text, recipe_sha, attribution_text in rows(path):
        for text, sha in ((context_text, context_sha), (recipe_text, recipe_sha)):
            if text is None:
                continue
            assert text == canonical_json(json.loads(text))
            assert sha == hashlib.sha256(text.encode()).hexdigest() == fingerprint(json.loads(text))
        if attribution_text is not None:
            assert attribution_text == canonical_json(json.loads(attribution_text))


async def test_a_new_store_is_marked_as_upgraded(tmp_path):
    LocalStore(tmp_path / "s.sqlite3").close()
    assert user_version(tmp_path / "s.sqlite3") == STORE_VERSION == 2


async def test_an_old_store_upgrades_to_execution_ids_once(tmp_path):
    path, candidate_id = await learned_store(tmp_path)
    current = rows(path)
    to_revisions(path)
    old = rows(path)
    assert len(current) == 3 and old != current
    assert old[2] == current[2]  # the imported version has nothing to rename
    assert '"revision_id"' in old[1][1] and '"origin":"revision"' in old[1][1] and '"source_revision_id"' in old[1][1]

    LocalStore(path).close()
    assert rows(path) == current  # the exact text and hashes the current SDK writes
    assert user_version(path) == STORE_VERSION
    assert not [row for row in rows(path) if any(text and "revision" in text for text in (row[1], row[3], row[5]))]

    LocalStore(path).close()  # a second open changes nothing
    assert rows(path) == current

    rsi, _ = make_rsi(tmp_path)
    try:
        view = await rsi.get_recipe_candidate(candidate_id)  # stored hashes verify
        assert view.recipe["steps"][0]["evidence_refs"][0]["execution_id"]
    finally:
        await rsi.aclose()


async def test_an_upgrade_stops_on_a_changed_hash_and_changes_nothing(tmp_path):
    path, candidate_id = await learned_store(tmp_path)
    to_revisions(path, break_hash_of=candidate_id)  # the second row: the first is rewritten before the upgrade stops
    old = rows(path)
    assert candidate_id == old[1][0]
    with pytest.raises(LocalStoreUpgradeError) as caught:
        LocalStore(path)
    message = str(caught.value)
    for part in (str(path), candidate_id, "recipe_json", "does not match its hash", "nothing was changed",
                 "previous cosens-sdk release", "move the file aside"):
        assert part in message
    assert (caught.value.path, caught.value.candidate_id, caught.value.column) == (str(path), candidate_id, "recipe_json")
    assert rows(path) == old and user_version(path) == 0


async def test_an_upgrade_stops_on_a_revision_it_cannot_resolve(tmp_path):
    path, _ = await learned_store(tmp_path)
    to_revisions(path, unknown_execution=True)
    old = rows(path)
    with pytest.raises(LocalStoreUpgradeError) as caught:
        LocalStore(path)
    message = str(caught.value)
    for part in ("revision_missing", old[0][0], "context_json", "nothing was changed", "move the file aside"):
        assert part in message
    assert isinstance(caught.value.__cause__, LookupError) and old[0][0] in str(caught.value.__cause__)
    assert rows(path) == old and user_version(path) == 0


async def test_a_store_from_a_newer_sdk_is_refused(tmp_path):
    path = tmp_path / "s.sqlite3"
    LocalStore(path).close()
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version={STORE_VERSION + 1}")
    with pytest.raises(LocalStoreUpgradeError, match="this store was written by a newer cosens-sdk") as caught:
        LocalStore(path)
    assert str(path) in str(caught.value)
    assert user_version(path) == STORE_VERSION + 1


async def test_a_0_6_store_gets_every_upgrade_in_one_open(tmp_path):
    path, _ = await learned_store(tmp_path)
    current, everything = rows(path), state(path)
    to_revisions(path)
    everything = receipts_kept_by_the_upgrade(everything, state(path))
    with sqlite3.connect(path) as db:  # 0.6 had no remote_version and published to Memory
        db.execute("ALTER TABLE recipe_heads DROP COLUMN remote_version")
        db.execute("UPDATE recipe_candidates SET publish_state='published'")
    LocalStore(path).close()
    assert rows(path) == current and user_version(path) == STORE_VERSION
    upgraded = state(path)
    assert {table: upgraded[table] for table in ("tasks", "executions", "task_recipes", "idempotency")} == {
        table: everything[table] for table in ("tasks", "executions", "task_recipes", "idempotency")}
    with sqlite3.connect(path) as db:
        assert "remote_version" in {row[1] for row in db.execute("PRAGMA table_info(recipe_heads)")}
        assert {row[0] for row in db.execute("SELECT publish_state FROM recipe_candidates")} == {"pending"}


async def test_a_version_1_store_upgrades_to_capability_tasks_once(tmp_path):
    path, candidate_id = await learned_store(tmp_path)
    current, shapes = state(path), {table: columns(path, table) for table in ("tasks", "executions", "task_recipes")}
    to_version_1(path)
    old = state(path)
    assert "mode" in [name for name, *_ in columns(path, "tasks")]
    [(_, _, _, old_task, _)] = old["tasks"]
    assert [a["role"] for a in json.loads(old_task)["artifacts"]] == ["original_input"]
    assert all('"context":' in row[5] and '"input"' not in row[5] for row in old["executions"])
    assert all("kind" not in json.loads(row[4]) for row in old["idempotency"] if row[1].startswith("start_"))
    assert '"workflow-recipe-context-v2"' in old["recipe_candidates"][1][6]
    learning_receipts = [row for row in old["idempotency"] if row[1] == "recipe_candidate"]
    assert learning_receipts and learning_receipts != [row for row in current["idempotency"] if row[1] == "recipe_candidate"]
    current = receipts_kept_by_the_upgrade(current, old)

    LocalStore(path).close()
    assert state(path) == current  # tasks, requests, receipts, instances and learning contexts as the current SDK writes them
    assert {table: columns(path, table) for table in shapes} == shapes
    assert user_version(path) == STORE_VERSION
    with sqlite3.connect(path) as db:
        tasks = [json.loads(text) for (text,) in db.execute("SELECT context_json FROM tasks")]
        executions = db.execute("SELECT id,kind,instance_id,request_json FROM executions ORDER BY rowid").fetchall()
    assert tasks == [{"objective": "Do the task", "artifacts": []}]
    (original, _, _, _), (correction, kind, instance, _) = executions
    assert kind == "revision" and instance == original != correction
    # The old task artifacts were the instance input: they stay under each request's input, never in its task context.
    for _, _, _, text in executions:
        request = json.loads(text)
        assert request["task_context"] == {"objective": "Do the task", "artifacts": [], "context_version": 1}
        assert [(a["name"], a["role"]) for a in request["input"]["artifacts"]] == [("brief.md", "original_input")]
    receipts = {row[1].split(":")[0]: json.loads(row[4]) for row in state(path)["idempotency"] if row[1].startswith("start_")}
    assert {name: receipt["kind"] for name, receipt in receipts.items()} == {
        "start_execution": "original", "start_revision": "revision"}
    assert all(receipt["request"]["input"]["artifacts"] and not receipt["request"]["task_context"]["artifacts"]
               for receipt in receipts.values())
    context = json.loads([row for row in rows(path) if row[0] == candidate_id][0][1])
    assert context["schema"] == "workflow-recipe-context-v3" and context["task_materials"] == []
    assert all("input" in entry and "context" not in entry for entry in context["history"])

    LocalStore(path).close()  # a second open changes nothing
    assert state(path) == current
    rsi, _ = make_rsi(tmp_path)
    try:
        view = await rsi.get_recipe_candidate(candidate_id)
        assert view.status == "candidate"  # stored hashes verify
        replayed = await rsi.start_recipe_candidate(context["task_id"], revision_id=view.revision_id)
        assert replayed.recipe_candidate_id == candidate_id and replayed.context_sha256 == view.context_sha256
        [recipe] = (await rsi.get_task(context["task_id"])).recipes
        assert (recipe["current_version"], recipe["latest_version"]) == (2, 2)
        with pytest.raises(ValueError, match="idempotency key reused with a different request"):
            await rsi.create_task(key="t1", objective="Do the task")  # a receipt of the old task request
    finally:
        await rsi.aclose()


async def test_a_version_1_upgrade_stops_on_a_recipe_shared_by_two_tasks(tmp_path):
    path, _ = await learned_store(tmp_path)
    to_version_1(path)
    with sqlite3.connect(path) as db:
        task_id, recipe_id = db.execute("SELECT task_id,recipe_id FROM task_recipes").fetchone()
        db.execute("INSERT INTO tasks VALUES('task_0','execute',?,1)",
                   (canonical_json({"objective": "B", "self_contained": False, "artifacts": []}),))
        db.execute("INSERT INTO task_recipes VALUES('task_0',?)", (recipe_id,))
    old = state(path)
    with pytest.raises(LocalStoreUpgradeError) as caught:
        LocalStore(path)
    message = str(caught.value)
    for part in (str(path), f"Recipes bound to more than one task: {recipe_id} (tasks task_0 {task_id})",
                 "From this release on a Recipe belongs to one task; nothing was changed", "previous cosens-sdk release",
                 "move the file aside"):
        assert part in message
    assert caught.value.path == str(path)
    assert state(path) == old and user_version(path) == 1
    assert "mode" in [name for name, *_ in columns(path, "tasks")]


async def test_a_version_1_upgrade_stops_on_a_changed_context_hash(tmp_path):
    path, candidate_id = await learned_store(tmp_path)
    to_version_1(path, break_context_hash_of=candidate_id)
    old = state(path)
    with pytest.raises(LocalStoreUpgradeError) as caught:
        LocalStore(path)
    for part in (candidate_id, "context_json", "does not match its hash", "nothing was changed"):
        assert part in str(caught.value)
    assert (caught.value.candidate_id, caught.value.column) == (candidate_id, "context_json")
    assert state(path) == old and user_version(path) == 1


async def test_a_version_1_upgrade_stops_on_a_revision_chain_it_cannot_follow(tmp_path):
    path, _ = await learned_store(tmp_path)
    to_version_1(path)
    with sqlite3.connect(path) as db:
        execution_id, request_text = db.execute("SELECT id,request_json FROM executions WHERE kind='revision'").fetchone()
        request = {**json.loads(request_text), "base_revision_id": "revision_missing"}
        db.execute("UPDATE executions SET request_json=? WHERE id=?", (canonical_json(request), execution_id))
    old = state(path)
    with pytest.raises(LocalStoreUpgradeError) as caught:
        LocalStore(path)
    for part in (execution_id, "revision_missing", "nothing was changed"):
        assert part in str(caught.value)
    assert state(path) == old and user_version(path) == 1


async def test_a_version_1_upgrade_gives_a_correction_of_a_correction_the_first_execution_as_instance(tmp_path):
    path, _ = await learned_store(tmp_path)
    rsi, _ = make_rsi(tmp_path)
    try:
        with sqlite3.connect(path) as db:
            (revision_id,) = db.execute("SELECT r.id FROM revisions r JOIN executions e ON e.id=r.execution_id "
                                        "WHERE e.kind='revision'").fetchone()
        await rsi.record_feedback(revision_id, feedback="still wrong", source="human", key="f2")
        await complete_execution(rsi, await rsi.start_revision(revision_id, key="r2"), key="r2-out")
    finally:
        await rsi.aclose()
    current = state(path)
    to_version_1(path)
    current = receipts_kept_by_the_upgrade(current, state(path))
    LocalStore(path).close()
    assert state(path) == current
    with sqlite3.connect(path) as db:
        executions = db.execute("SELECT id,kind,instance_id FROM executions ORDER BY rowid").fetchall()
    (original, _, _), *corrections = executions
    assert [(kind, instance) for _, kind, instance in corrections] == [("revision", original)] * 2
