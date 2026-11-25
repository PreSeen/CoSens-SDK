"""Upload local Recipe ZIPs and adopt the cloud IDs from durable receipts."""

from __future__ import annotations

import hashlib
import re

from .cloud import _read_recipe_zip, _validate_recipe_upload
from .models import Conflict, InvalidState, RSIError
from .recipe_identity import (
    BOOTSTRAP,
    IDENTITY,
    PENDING,
    RECEIPT,
    journal_entry,
    promote_recipe_identity,
    recipe_id_for,
    recipe_object_relation,
    require_idle_recipe,
    require_unclaimed_upload_target,
    version_id_for,
)
from .recipe_package import assert_recipe_package_matches
from .task import assert_frozen_package_matches, freeze_task_package, is_approved, task_chain

_TERMINAL_UPLOAD_STATUS = {400, 403, 404, 409, 413, 422}


def _same_request(conn, plan, recipe_id, version_id, zip_sha256, key, destination):
    return (
        recipe_id_for(conn, plan["local_recipe_id"]) == recipe_id
        and version_id_for(conn, plan["local_recipe_version_id"]) == version_id
        and plan["zip_sha256"] == zip_sha256
        and (key is None or plan["idempotency_key"] == key)
        and all(plan.get(field) == value for field, value in destination.items())
    )


def _saved_receipt(conn, recipe_id, version_id, key, destination, zip_sha256=None, *, target_recipe_id=None):
    saved = journal_entry(conn, RECEIPT, version_id)
    if saved is None:
        return None
    plan, receipt = saved
    if target_recipe_id is not None and target_recipe_id != receipt["recipe_id"]:
        raise Conflict("local Recipe version already has a receipt for a different cloud Recipe")
    if not _same_request(
        conn, plan, recipe_id, version_id,
        plan["zip_sha256"] if zip_sha256 is None else zip_sha256, key, destination,
    ):
        raise Conflict(
            "local Recipe version already has a cloud receipt for a different ZIP, key or destination"
        )
    return receipt


def _receipt(value, target):
    fields = ("recipe_id", "recipe_version_id")
    if not isinstance(value, dict) or any(
        not isinstance(value.get(field), str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value[field])
        for field in fields
    ):
        raise RSIError("CoSens RSI returned an invalid Recipe upload receipt")
    if target is not None and value["recipe_id"] != target:
        raise RSIError("CoSens RSI returned a receipt for a different Recipe")
    return {field: value[field] for field in fields}


def _plan_upload(store, conn, recipe_id, version_id, zip_sha256, key, destination, target_recipe_id):
    require_idle_recipe(conn, recipe_id, version_id)
    pending = journal_entry(conn, PENDING, recipe_id)
    if pending:
        target = pending[0]["cloud_recipe_id"]
        if target_recipe_id is not None and target_recipe_id != target:
            raise Conflict("pending local Recipe upload has a different cloud Recipe target")
        if not _same_request(
            conn, pending[0], recipe_id, version_id, zip_sha256, key, destination
        ):
            raise Conflict(
                "retry the pending local Recipe upload with the same version, ZIP and idempotency key and destination"
            )
        require_unclaimed_upload_target(conn, target, recipe_id)
        return pending[0]
    identity = journal_entry(conn, IDENTITY, recipe_id)
    if identity and identity[0] != destination:
        raise Conflict(
            "local Recipe is already linked to a different cloud destination"
        )
    if identity and target_recipe_id is not None and target_recipe_id != recipe_id:
        raise Conflict("local Recipe is already linked to a different cloud Recipe")
    target = recipe_id if identity else target_recipe_id
    if target is not None:
        if recipe_id_for(conn, target) != target or (target != recipe_id and (
            journal_entry(conn, IDENTITY, target) or conn.execute(
                f"SELECT 1 FROM {recipe_object_relation(conn)} WHERE id=?", (target,)
            ).fetchone()
        )):
            raise Conflict("cloud Recipe target is already linked to another local Recipe or alias")
        require_unclaimed_upload_target(conn, target, recipe_id)
    if key is None:
        parts = f"{recipe_id}\0{version_id}\0{zip_sha256}"
        if target is not None:
            parts += f"\0{target}"
        key = "local-recipe-upload-" + hashlib.sha256(parts.encode()).hexdigest()
    _validate_recipe_upload(target, key)
    plan = {
        "local_recipe_id": recipe_id,
        "local_recipe_version_id": version_id,
        "zip_sha256": zip_sha256,
        "idempotency_key": key,
        "cloud_recipe_id": target,
        **destination,
    }
    store.remember(conn, PENDING, recipe_id, plan, {})
    return plan


def upload_task(store, task_id, key, destination, cloud_factory, *, target_recipe_id=None):
    _validate_recipe_upload(target_recipe_id, key)
    with store.transaction() as conn:
        execution = task_chain(store, conn, task_id)[-1]
        if not is_approved(execution):
            raise InvalidState("Only an approved final Task Recipe version can be uploaded")
        recipe_id = recipe_id_for(conn, execution["recipe_id"])
        version_id = version_id_for(conn, execution["recipe_version_id"])
        receipt = _saved_receipt(conn, recipe_id, version_id, key, destination, target_recipe_id=target_recipe_id)
        if receipt is not None:
            return receipt
        pending = journal_entry(conn, PENDING, recipe_id)
        pending_sha256 = None
        if pending and version_id_for(conn, pending[0]["local_recipe_version_id"]) == version_id:
            pending_sha256 = pending[0]["zip_sha256"]
        package = freeze_task_package(store, conn, execution, expected_sha256=pending_sha256)
        if package is None:
            raise Conflict("retry the pending local Recipe upload with the same version, ZIP and idempotency key and destination")

    filename, zip_bytes = _read_recipe_zip(package)
    cloud = cloud_factory()
    try:
        return upload_recipe(
            store, cloud, filename, zip_bytes, recipe_id, version_id, key,
            target_recipe_id=target_recipe_id,
        )
    finally:
        cloud.close()


def upload_recipe(store, cloud, filename, zip_bytes, recipe_id, version_id, key, *, target_recipe_id=None):
    _validate_recipe_upload(target_recipe_id, key)
    zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    destination = cloud._recipe_upload_destination()
    with store.transaction() as conn:
        recipe_id = recipe_id_for(conn, recipe_id)
        version_id = version_id_for(conn, version_id)
        version = store.version_in(conn, recipe_id, version_id)
        receipt = _saved_receipt(
            conn, recipe_id, version_id, key, destination, zip_sha256,
            target_recipe_id=target_recipe_id,
        )
        if receipt is not None:
            return receipt
        if journal_entry(conn, BOOTSTRAP, version_id):
            raise InvalidState("cloud Recipe base version is already published")
        if version.get("provenance", {}).get("execution_id") and not conn.execute(
            """SELECT 1 FROM executions
            WHERE recipe_id=? AND recipe_version_id=? AND status='completed'
              AND json_extract(details,'$.result.test_status')='passed'
              AND json_extract(details,'$.result.published')=1
              AND json_extract(details,'$.result.provisional')=0 LIMIT 1""",
            (recipe_id, version_id),
        ).fetchone():
            raise InvalidState("Only an approved final Task Recipe version can be uploaded")
        assert_frozen_package_matches(conn, version_id, zip_sha256)
        recipe = store.get_in(conn, recipe_id, "recipe")
        directory = store.root / recipe["storage_path"] / version_id
        entrypoint = directory / "RECIPE.md"
        if not entrypoint.exists() and not any(
            path.is_symlink() for path in (entrypoint, directory, directory.parent, directory.parent.parent)
        ):
            store._write_recipe_file(recipe["storage_path"], version)
        assert_recipe_package_matches(directory, zip_bytes)
        plan = _plan_upload(
            store, conn, recipe_id, version_id, zip_sha256, key, destination, target_recipe_id,
        )

    target = plan["cloud_recipe_id"]
    try:
        value = cloud._recipes_upsert_bytes(
            filename,
            zip_bytes,
            recipe_id=target,
            idempotency_key=plan["idempotency_key"],
        )
    except (RSIError, ValueError) as error:
        if getattr(error, "status", None) in _TERMINAL_UPLOAD_STATUS:
            with store.transaction() as conn:
                pending = journal_entry(conn, PENDING, recipe_id)
                if pending and pending[0] == plan:
                    conn.execute(
                        "DELETE FROM idempotency WHERE scope=? AND key=?",
                        (PENDING, recipe_id),
                    )
        raise

    receipt = _receipt(value, target)
    with store.transaction() as conn:
        # Another caller can commit the same receipt while the request is in flight.
        saved = journal_entry(conn, RECEIPT, version_id_for(conn, version_id))
        if saved:
            if saved != (plan, receipt):
                raise Conflict(
                    "cloud upload receipt conflicts with the saved local Recipe identity"
                )
            return saved[1]
        pending = journal_entry(conn, PENDING, recipe_id)
        if not pending or pending[0] != plan:
            raise Conflict("local Recipe upload reservation changed")
        require_idle_recipe(conn, recipe_id, version_id)
        promote_recipe_identity(store, conn, recipe_id, version_id, receipt)
        if not journal_entry(conn, IDENTITY, receipt["recipe_id"]):
            store.remember(conn, IDENTITY, receipt["recipe_id"], destination, {})
        store.remember(conn, RECEIPT, receipt["recipe_version_id"], plan, receipt)
        conn.execute(
            "DELETE FROM idempotency WHERE scope=? AND key=?", (PENDING, recipe_id)
        )
    return receipt
