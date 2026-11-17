"""On-demand background worker; no HTTP port, Redis or permanent daemon."""

from __future__ import annotations

import argparse
import sqlite3
import threading
import time

from .models import LeaseLost, RSIError
from .store import uid


def failure_details(exc, stage):
    """Keep actionable status metadata, never upstream bodies, headers or prompts."""
    from openai import APIStatusError

    details = {
        "code": exc.code if isinstance(exc, RSIError) else "execution_failed",
        "type": type(exc).__name__,
        "stage": stage,
    }
    if isinstance(exc, APIStatusError):
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


def drain(client, *, runtime=None, owner=None, slot=None):
    from .engine import Engine
    from .runtime import LangChainRuntime

    store, config = client.store, client.config
    owner = owner or uid("worker")
    slot = slot or store.reserve(
        "worker", owner, config.max_executions, config.lease_seconds
    )
    if slot is None:
        return {"processed": 0}
    stop = threading.Event()
    lost = threading.Event()
    active = [None]

    def check_alive():
        if lost.is_set():
            raise LeaseLost("worker heartbeat failed")
        if active[0] is not None:
            execution_id, fence, revision = active[0]
            with store.transaction() as conn:
                store.guard(conn, execution_id, owner, fence, revision)

    def heartbeat():
        while not stop.wait(config.lease_seconds / 3):
            try:
                store.heartbeat(owner, config.lease_seconds, worker_slot=slot)
            except (sqlite3.Error, LeaseLost):
                lost.set()
                return

    processed = 0
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    runtime = runtime or LangChainRuntime(client, owner, check_alive)
    try:
        store.heartbeat(owner, config.lease_seconds, worker_slot=slot)
        idle_since = time.monotonic()
        while not lost.is_set():
            claim = store.claim(owner, config.lease_seconds, config.max_executions)
            if claim is None:
                # Brief idle grace handles simultaneous hook dispatch; exits once drained.
                if time.monotonic() - idle_since >= 1:
                    # Release under the same write lock as the final empty check: an
                    # enqueue after this check can reserve our released slot and spawn.
                    with store.transaction() as conn:
                        pending = conn.execute(
                            "SELECT 1 FROM jobs WHERE status='queued' OR (status='running' AND lease_until<?) LIMIT 1",
                            (time.time(),),
                        ).fetchone()
                        if not pending:
                            conn.execute("DELETE FROM slots WHERE token=?", (slot,))
                            break
                    idle_since = time.monotonic()
                time.sleep(0.05)
                continue
            idle_since = time.monotonic()
            execution_id, fence = claim
            # Tools check cancellation and leases; Engine checks input revisions
            # before committing a stage and restarts analysis when needed.
            active[0] = (execution_id, fence, None)
            try:
                Engine(client, runtime, owner, fence, execution_id).run()
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
            processed += 1
    finally:
        stop.set()
        thread.join(timeout=config.lease_seconds)
        store.release(slot)
    return {"processed": processed}


def main():
    from .client import RSIClient

    parser = argparse.ArgumentParser(
        description="Drain the local RSI queue and exit; no server is started"
    )
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--owner")
    parser.add_argument("--slot")
    args = parser.parse_args()
    if bool(args.owner) != bool(args.slot):
        parser.error("owner and slot must be supplied together")
    client = RSIClient(data_dir=args.data_dir, auto_start=False)
    drain(client, owner=args.owner, slot=args.slot)


if __name__ == "__main__":
    main()
