"""Upload local Recipe ZIPs and adopt the cloud IDs from durable receipts."""

from __future__ import annotations

import hashlib
import re

from .cloud import _validate_recipe_upload
from .models import Conflict, RSIError
from .recipe_identity import (
    IDENTITY,
    PENDING,
    RECEIPT,
    journal_entry,
    promote_recipe_identity,
    recipe_id_for,
    require_idle_recipe,
    version_id_for,
)

_TERMINAL_UPLOAD_STATUS = {400, 403, 404, 409, 413, 422}


def _same_request(conn, plan, recipe_id, version_id, zip_sha256, key, destination):
    return (
        recipe_id_for(conn, plan["local_recipe_id"]) == recipe_id
        and version_id_for(conn, plan["local_recipe_version_id"]) == version_id
        and plan["zip_sha256"] == zip_sha256
        and (key is None or plan["idempotency_key"] == key)
        and all(plan.get(field) == value for field, value in destination.items())
    )


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


def _plan_upload(store, conn, recipe_id, version_id, zip_sha256, key, destination):
    require_idle_recipe(conn, recipe_id, version_id)
    pending = journal_entry(conn, PENDING, recipe_id)
    if pending:
        if not _same_request(
            conn, pending[0], recipe_id, version_id, zip_sha256, key, destination
        ):
            raise Conflict(
                "retry the pending local Recipe upload with the same version, ZIP and idempotency key and destination"
            )
        return pending[0]
    identity = journal_entry(conn, IDENTITY, recipe_id)
    if identity and identity[0] != destination:
        raise Conflict(
            "local Recipe is already linked to a different cloud destination"
        )
    if key is None:
        parts = f"{recipe_id}\0{version_id}\0{zip_sha256}"
        key = "local-recipe-upload-" + hashlib.sha256(parts.encode()).hexdigest()
    target = recipe_id if identity else None
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


def upload_recipe(store, cloud, filename, zip_bytes, recipe_id, version_id, key):
    zip_sha256 = hashlib.sha256(zip_bytes).hexdigest()
    destination = cloud._recipe_upload_destination()
    with store.transaction() as conn:
        recipe_id = recipe_id_for(conn, recipe_id)
        version_id = version_id_for(conn, version_id)
        store.version_in(conn, recipe_id, version_id)
        saved = journal_entry(conn, RECEIPT, version_id)
        if saved:
            if not _same_request(
                conn, saved[0], recipe_id, version_id, zip_sha256, key, destination
            ):
                raise Conflict(
                    "local Recipe version already has a cloud receipt for a different ZIP, key or destination"
                )
            return saved[1]
        plan = _plan_upload(
            store, conn, recipe_id, version_id, zip_sha256, key, destination
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
        if target is None:
            store.remember(conn, IDENTITY, receipt["recipe_id"], destination, {})
        store.remember(conn, RECEIPT, receipt["recipe_version_id"], plan, receipt)
        conn.execute(
            "DELETE FROM idempotency WHERE scope=? AND key=?", (PENDING, recipe_id)
        )
    return receipt
