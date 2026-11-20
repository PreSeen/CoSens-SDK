"""On-demand background worker; no HTTP port, Redis or permanent daemon."""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time

from .models import LeaseLost, LocalConfig, RSIError
from .store import uid


def failure_details(exc, stage):
    """Keep actionable status metadata, never upstream bodies, headers or prompts."""
    from .chat_completions import ModelStatusError

    details = {
        "code": exc.code if isinstance(exc, RSIError) else "execution_failed",
        "type": type(exc).__name__,
        "stage": stage,
    }
    if isinstance(exc, ModelStatusError):
        details["upstream_status"] = exc.status_code
        details["code"] = (
            "model_payment_required"
            if exc.status_code == 402
            else "model_rate_limited"
            if exc.status_code == 429
            else "model_unavailable"
            if exc.status_code >= 500
            else "model_request_rejected"
        )
    return details


def drain(client, *, runtime=None, owner=None, slot=None, recipe_id=None):
    from ._queue_client import RSIClient
    from .engine import Engine
    from .runtime import ChatRuntime

    store, config = client.store, client.config
    credentials = None if runtime is not None else client.credential_names()
    owner = owner or uid("worker")
    detached = slot is not None
    slot = slot or store.reserve(
        "worker", owner, config.max_executions, config.lease_seconds
    )
    if slot is None:
        return {"processed": 0}
    stop = threading.Event()
    lost = threading.Event()
    active = [None]
    lease_seconds = config.lease_seconds

    def check_alive():
        if lost.is_set():
            raise LeaseLost("worker heartbeat failed")
        if active[0] is not None:
            execution_id, fence, revision = active[0]
            with store.transaction() as conn:
                store.guard(conn, execution_id, owner, fence, revision)

    def heartbeat():
        # One third of the minimum supported lease.
        while not stop.wait(1):
            try:
                store.heartbeat(owner, lease_seconds, worker_slot=slot)
            except (sqlite3.Error, LeaseLost):
                lost.set()
                return

    processed = 0
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        store.heartbeat(owner, config.lease_seconds, worker_slot=slot)
        idle_since = time.monotonic()
        while not lost.is_set():
            claim = store.claim(
                owner,
                config.lease_seconds,
                config.max_executions,
                credentials,
                recipe_id=recipe_id,
            )
            if claim is None:
                # Brief idle grace handles simultaneous hook dispatch; exits once drained.
                if time.monotonic() - idle_since >= 1:
                    # Release under the same write lock as the final empty check: an
                    # enqueue after this check can reserve our released slot and spawn.
                    with store.transaction() as conn:
                        # Detached workers can take over a running job after its lease expires.
                        pending = (
                            store.has_pending(credentials, recipe_id=recipe_id)
                            if detached
                            or credentials is not None
                            or recipe_id is not None
                            else conn.execute(
                                "SELECT 1 FROM executions WHERE status='queued' "
                                "OR (status='running' AND lease_until<?) LIMIT 1",
                                (time.time(),),
                            ).fetchone()
                        )
                        if not pending:
                            conn.execute("DELETE FROM slots WHERE token=?", (slot,))
                            break
                    idle_since = time.monotonic()
                time.sleep(0.2 if detached else 0.05)
                continue
            idle_since = time.monotonic()
            execution_id, fence = claim
            # Tools check cancellation and leases; Engine checks input revisions
            # before committing a stage and restarts analysis when needed.
            active[0] = (execution_id, fence, None)
            try:
                work = RSIClient(
                    data_dir=client.data_dir,
                    config=store.get(execution_id, "execution")["runtime_config"],
                    auto_start=False,
                )
                lease_seconds = work.config.lease_seconds
                store.heartbeat(owner, lease_seconds, worker_slot=slot)
                agent = runtime or ChatRuntime(work, owner, check_alive)
                Engine(work, agent, owner, fence, execution_id).run()
            except LeaseLost:
                pass
            except Exception as exc:  # noqa: BLE001 - isolate arbitrary model/tool failures at the job boundary
                # Provider exceptions may contain credentials or raw prompts. Persist
                # only the class name and a stable diagnostic code, never repr(exc).
                try:
                    store.progress(
                        execution_id,
                        owner,
                        fence,
                        None,
                        status="failed",
                        stage="failed",
                        error=failure_details(
                            exc, store.get(execution_id, "execution")["stage"]
                        ),
                    )
                except LeaseLost:
                    pass
            finally:
                active[0] = None
                lease_seconds = config.lease_seconds
            processed += 1
    finally:
        stop.set()
        thread.join(timeout=config.lease_seconds)
        store.release(slot)
    return {"processed": processed}


def wait_for_worker_slot(client, owner, waiting_slot, *, recipe_id=None):
    store, config = client.store, client.config
    credentials = client.credential_names()
    next_heartbeat = 0
    try:
        while True:
            if time.monotonic() >= next_heartbeat:
                store.heartbeat(owner, config.lease_seconds, worker_slot=waiting_slot)
                next_heartbeat = time.monotonic() + config.lease_seconds / 3
            with store.transaction() as conn:
                if not store.has_pending(credentials, recipe_id=recipe_id):
                    conn.execute("DELETE FROM slots WHERE token=?", (waiting_slot,))
                    return None

            slot = store.reserve(
                "worker", owner, config.max_executions, config.lease_seconds
            )
            if slot:
                return slot
            time.sleep(0.25)
    finally:
        store.release(waiting_slot)


def main():
    from ._queue_client import RSIClient

    parser = argparse.ArgumentParser(
        description="Drain the local RSI queue and exit; no server is started"
    )
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--owner")
    parser.add_argument("--slot")
    parser.add_argument("--execution-id")
    parser.add_argument("--recipe-id")
    parser.add_argument("--wait-for-slot", action="store_true")
    args = parser.parse_args()
    if bool(args.owner) != bool(args.slot) or (args.wait_for_slot and not args.slot):
        parser.error("owner and slot must be supplied together")
    reader = RSIClient(data_dir=args.data_dir, auto_start=False)
    if args.execution_id:
        config = reader.execution_get(args.execution_id)["runtime_config"]
    else:
        pending = reader.store.pending_execution(recipe_id=args.recipe_id)
        if not pending:
            return
        config = LocalConfig.model_validate_json(pending["config"])
    client = RSIClient(data_dir=args.data_dir, config=config, auto_start=False)
    slot = args.slot
    if args.wait_for_slot:
        slot = wait_for_worker_slot(client, args.owner, slot, recipe_id=args.recipe_id)
        if slot is None:
            return
    drain(client, owner=args.owner, slot=slot, recipe_id=args.recipe_id)


if __name__ == "__main__":
    main()
