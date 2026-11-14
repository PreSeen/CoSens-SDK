"""Opening a LocalRSI store written before evidence references named executions upgrades it once."""
import hashlib
import json
import sqlite3

import pytest

from cosens.local.store import LocalStore, canonical_json, fingerprint
from local_rsi_helpers import complete_execution, first_revision, make_rsi, recipe_reply

COLUMNS = "id,context_json,context_sha,recipe_json,recipe_sha,attribution_json"


def attribution_citing_an_event(payload):
    history = payload["context"]["history"]
    event = {"execution_id": history[-1]["execution_id"], "event_id": history[-1]["events"][0]["id"]}
    feedback = {"feedback_id": history[0]["feedback"][0]["feedback_id"]}
    return {"actionable": True, "verified_success": False, "new_evidence": [], "limitations": "",
            "possible_causes": [{"reason": "r", "uncertainty": "u", "evidence_refs": [event, feedback]}]}


async def learned_store(tmp_path):
    """V1 learned from an execution, then V2 from its correction after feedback; V2's Recipe and attribution cite events."""
    rsi, _ = make_rsi(tmp_path, recipe_reply("V1"), attribution_citing_an_event, recipe_reply("V2"))
    task, _, done = await first_revision(rsi)
    first = await rsi.start_recipe_candidate(task.task_id, revision_id=done.revision_id)
    assert (await rsi.wait_workflow_job("recipe_candidate", first.recipe_candidate_id)).status == "candidate"
    await rsi.record_feedback(done.revision_id, feedback="wrong", source="human", key="f")
    started = await rsi.start_revision(done.revision_id, key="r")
    again = await complete_execution(rsi, started, key="r-out")
    candidate = await rsi.start_recipe_candidate(task.task_id, revision_id=again.revision_id)
    learned = await rsi.wait_workflow_job("recipe_candidate", candidate.recipe_candidate_id)
    assert learned.status == "candidate"
    await rsi.aclose()
    return tmp_path / "state.sqlite3", learned.recipe_candidate_id


def rows(path):
    with sqlite3.connect(path) as db:
        return db.execute(f"SELECT {COLUMNS} FROM recipe_candidates ORDER BY rowid").fetchall()


def user_version(path):
    with sqlite3.connect(path) as db:
        return db.execute("PRAGMA user_version").fetchone()[0]


def to_revisions(path, *, break_hash_of=None, unknown_execution=False):
    """Write the store back as an earlier SDK did: revision keys, history origin "revision", hashes of that text."""
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
    assert user_version(tmp_path / "s.sqlite3") == 1


async def test_an_old_store_upgrades_to_execution_ids_once(tmp_path):
    path, candidate_id = await learned_store(tmp_path)
    current = rows(path)
    to_revisions(path)
    old = rows(path)
    assert len(current) == 2 and old != current
    assert '"revision_id"' in old[1][1] and '"origin":"revision"' in old[1][1] and '"source_revision_id"' in old[1][1]

    LocalStore(path).close()
    assert rows(path) == current  # the exact text and hashes the current SDK writes
    assert user_version(path) == 1
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
    with pytest.raises(ValueError, match="hash"):
        LocalStore(path)
    assert rows(path) == old and user_version(path) == 0


async def test_an_upgrade_stops_on_a_revision_it_cannot_resolve(tmp_path):
    path, _ = await learned_store(tmp_path)
    to_revisions(path, unknown_execution=True)
    old = rows(path)
    with pytest.raises(LookupError, match="revision_missing"):
        LocalStore(path)
    assert rows(path) == old and user_version(path) == 0
