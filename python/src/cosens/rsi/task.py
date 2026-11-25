"""Project local Tasks from explicitly linked Executions."""

import hashlib
import os
import re
import tempfile

from .models import Conflict, NotFound
from .recipe_identity import (
    RECEIPT,
    VERSION_ALIAS,
    journal_entry,
    recipe_id_for,
    recipe_object_relation,
    version_id_for,
)

_PACKAGE_SCOPE = "task_final_package"


def _frozen_package(conn, task_id):
    entry = journal_entry(conn, _PACKAGE_SCOPE, task_id)
    if entry:
        return entry[0]
    entry = journal_entry(conn, "task_recipe_package", task_id)
    if entry:
        return {**entry[0], **entry[1]}
    return None


def is_approved(execution):
    result = execution.get("result") or {}
    return (
        execution["status"] == "completed"
        and result.get("test_status") == "passed"
        and result.get("published") is True
        and result.get("provisional") is False
        and execution.get("recipe_id") is not None
        and execution.get("recipe_version_id") is not None
    )


def continuation_ids(execution):
    explicit = execution.get("continuation_execution_id")
    if explicit:
        return [explicit]
    return [
        answer["continuation_execution_id"]
        for answer in execution["answers"].values()
        if answer.get("continuation_execution_id")
    ]


def root_id(store, conn, execution):
    seen = {execution["id"]}
    while parent_id := execution.get("base_execution_id"):
        if parent_id in seen:
            raise Conflict("invalid Task continuation cycle")
        try:
            parent = store.get_in(conn, parent_id, "execution")
        except NotFound:
            break
        if execution["id"] not in continuation_ids(parent):
            break
        seen.add(parent_id)
        execution = parent
    return execution["id"]


def task_chain(store, conn, task_id):
    current = store.get_in(conn, task_id, "execution")
    if root_id(store, conn, current) != task_id:
        raise NotFound(f"Task not found: {task_id}")
    chain = [current]
    seen = {task_id}
    while candidates := continuation_ids(current):
        children = []
        for child_id in candidates:
            if child_id in seen:
                raise Conflict("invalid Task continuation cycle")
            try:
                child = store.get_in(conn, child_id, "execution")
            except NotFound:
                continue
            if child.get("base_execution_id") == current["id"]:
                children.append(child)
        if not children:
            break
        current = max(children, key=lambda row: (row["created_at"], row["id"]))
        chain.append(current)
        seen.add(current["id"])
    return chain


def _question(execution, question):
    return {
        "execution_id": execution["id"],
        "question_id": question["id"],
        "question_revision": question["revision"],
        "question": question["question"],
        "options": question["options"],
    }


def _question_key(question):
    return f"{question['id']}:{question['revision']}"


def _pending(execution):
    if execution["status"] in {"failed", "cancelled"}:
        return []
    return [
        _question(execution, question)
        for question in execution["questions"]
        if question["revision"] == execution["input_revision"]
        and _question_key(question) not in execution["answers"]
    ]


def summary(store, conn, chain):
    root, current = chain[0], chain[-1]
    return {
        "task_id": root["id"],
        "execution_id": current["id"],
        "name": store.execution_recipe_name(conn, current) or "首次学习",
        **{field: current[field] for field in ("status", "stage", "progress_percent", "updated_at")},
        "test_status": (current.get("result") or {}).get("test_status"),
        "created_at": root["created_at"],
        "pending_question_count": len(_pending(current)),
    }


def task_list(store):
    with store.connect() as conn:
        conn.execute("BEGIN")
        rows = conn.execute(
            """SELECT e.* FROM recipes r JOIN executions e ON e.id=r.id
            WHERE e.archived_at IS NULL ORDER BY e.created_at DESC, e.id DESC"""
        ).fetchall()
        return [
            summary(store, conn, task_chain(store, conn, row["id"]))
            for row in rows
            if root_id(store, conn, store._record(row)) == row["id"]
        ]


def _save_task_recipe(store, conn, task_id, execution, path=None):
    if recipe_object_relation(conn) != "recipe_objects":
        return
    recipe_id = recipe_id_for(conn, execution.get("recipe_id"))
    version_id = version_id_for(conn, execution["recipe_version_id"]) if is_approved(execution) else None
    conn.execute(
        """INSERT INTO recipes(id,recipe_id,recipe_version_id,recipe_zip_path) VALUES(?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET recipe_id=excluded.recipe_id,
          recipe_version_id=excluded.recipe_version_id, recipe_zip_path=excluded.recipe_zip_path""",
        (task_id, recipe_id, version_id, path.relative_to(store.root).as_posix() if path else None),
    )


def write_task_recipe(store, conn, execution, path=None):
    for child_id in continuation_ids(execution):
        child = conn.execute("SELECT base_execution_id FROM executions WHERE id=?", (child_id,)).fetchone()
        if child and child[0] == execution["id"]:
            return
    _save_task_recipe(store, conn, root_id(store, conn, execution), execution, path)


def _read_task_zip(path):
    from .recipe_package import MAX_ZIP_BYTES

    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as stream:
        content = stream.read(MAX_ZIP_BYTES + 1)
    return content if len(content) <= MAX_ZIP_BYTES else None


def backfill_task_recipes(store, conn):
    for row in conn.execute("SELECT * FROM executions ORDER BY created_at,id"):
        root = store._record(row)
        task_id = root["id"]
        if root_id(store, conn, root) != task_id:
            continue
        execution = task_chain(store, conn, task_id)[-1]
        path = None
        if is_approved(execution) and re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            version_id = version_id_for(conn, execution["recipe_version_id"])
            journal = _frozen_package(conn, task_id)
            candidate = store.root / "task-packages" / f"{task_id}.zip"
            try:
                if (
                    journal and journal["execution_id"] == execution["id"]
                    and version_id_for(conn, journal["version_id"]) == version_id
                ):
                    expected = journal["sha256"]
                else:
                    receipt = journal_entry(conn, RECEIPT, version_id)
                    expected = receipt[0].get("zip_sha256") if receipt else None
                cached = _read_task_zip(candidate) if expected else None
                if cached and hashlib.sha256(cached).hexdigest() == expected:
                    path = candidate
            except OSError:
                pass
        _save_task_recipe(store, conn, task_id, execution, path)


def freeze_task_package(store, conn, execution, *, expected_sha256=None):
    from .recipe_package import build_recipe_package

    task_id = root_id(store, conn, execution)
    path = store.root / "task-packages" / f"{task_id}.zip"
    if path.parent.is_symlink() or path.is_symlink():
        raise Conflict("invalid Task Recipe ZIP storage path")
    recipe = store.get_in(conn, execution["recipe_id"], "recipe")
    version = store.version_in(conn, recipe["id"], execution["recipe_version_id"])
    saved = _frozen_package(conn, task_id)
    if saved:
        if (
            saved["execution_id"] != execution["id"]
            or version_id_for(conn, saved["version_id"]) != version["id"]
        ):
            raise Conflict("Task already has a different final Recipe version")
        cached = _read_task_zip(path)
        if cached is None:
            raise NotFound("Task's final Recipe ZIP is missing")
        if hashlib.sha256(cached).hexdigest() != saved["sha256"]:
            raise Conflict("Task's final Recipe ZIP contents changed")
        if expected_sha256 is not None and expected_sha256 != saved["sha256"]:
            return None
        _save_task_recipe(store, conn, task_id, execution, path)
        return path
    package = build_recipe_package(store.root / recipe["storage_path"] / version["id"])
    sha256 = hashlib.sha256(package).hexdigest()
    if expected_sha256 is not None and expected_sha256 != sha256:
        return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = stream.name
            stream.write(package)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    store.remember(conn, _PACKAGE_SCOPE, task_id, {
        "execution_id": execution["id"],
        "version_id": version["id"],
        "sha256": sha256,
    }, {})
    _save_task_recipe(store, conn, task_id, execution, path)
    return path


def assert_frozen_package_matches(conn, version_id, zip_sha256):
    mismatch = conn.execute(
        """SELECT 1 FROM idempotency AS package
        LEFT JOIN idempotency AS alias
          ON alias.scope=? AND alias.key=json_extract(package.request,'$.version_id')
        WHERE package.scope=?
          AND COALESCE(json_extract(alias.result,'$.id'),json_extract(package.request,'$.version_id'))=?
          AND json_extract(package.request,'$.sha256')<>? LIMIT 1""",
        (VERSION_ALIAS, _PACKAGE_SCOPE, version_id, zip_sha256),
    ).fetchone()
    if mismatch:
        raise Conflict("Recipe ZIP does not match the frozen Task archive")


def detail(store, conn, chain):
    current = chain[-1]
    history = {}
    answers = {}
    for execution in chain:
        for question in [*execution.get("question_history", ()), *execution["questions"]]:
            history.setdefault(_question_key(question), _question(execution, question))
        for key, answer in execution["answers"].items():
            answers.setdefault(key, (execution["id"], answer))
    for key, (execution_id, answer) in answers.items():
        suffix = f":{answer['question_revision']}"
        if key not in history and key.endswith(suffix):
            history[key] = {
                "execution_id": execution_id,
                "question_id": key.removesuffix(suffix),
                "question_revision": answer["question_revision"],
                "question": answer["question"],
                "options": answer.get("selected_options", []),
            }
    for key, question in history.items():
        _, answer = answers.get(key, (None, None))
        question["answer"] = (
            {name: value for name, value in answer.items() if name != "continuation_execution_id"}
            if answer is not None else None
        )
    recipe_id = recipe_id_for(conn, current.get("recipe_id"))
    version_id = version_id_for(conn, current.get("recipe_version_id"))
    approved = is_approved(current)
    receipt = journal_entry(conn, RECEIPT, version_id) if approved else None
    artifact = None
    if approved:
        path = None
        if receipt is None:
            path = freeze_task_package(store, conn, current)
        elif receipt_sha256 := receipt[0].get("zip_sha256"):
            try:
                path = freeze_task_package(store, conn, current, expected_sha256=receipt_sha256)
            except (Conflict, NotFound, OSError, ValueError):
                # An already-published version can still be downloaded from the cloud.
                pass
        if path is not None:
            artifact = {
                "zip_path": str(path),
                "recipe_id": recipe_id,
                "recipe_version_id": version_id,
            }
    if artifact is None:
        _save_task_recipe(store, conn, chain[0]["id"], current)
    final_version_id = version_id if artifact else None
    if receipt:
        recipe_id = receipt[1]["recipe_id"]
        final_version_id = receipt[1]["recipe_version_id"]
    return {
        **summary(store, conn, chain),
        "execution_ids": [execution["id"] for execution in chain],
        "pending_questions": _pending(current),
        "clarification_history": list(history.values()),
        "result": current.get("result"),
        "error": current.get("error"),
        "artifact": artifact,
        "recipe_id": recipe_id,
        "recipe_version_id": final_version_id,
        "recipe_zip_path": artifact["zip_path"] if artifact else None,
    }
