"""Local orchestration helpers shared by transparent and explicit runs."""

import asyncio
import time
from copy import deepcopy

from .errors import RSIRecoveryRequired
from .models import result


def freeze_run_context(prepared, run):
    """Validate the complete prepared context before allowing native execution."""
    if not isinstance(prepared, dict) or not isinstance(prepared.get("recipe"), dict):
        raise RSIRecoveryRequired("Prepared Recipe is missing or invalid")
    if not isinstance(prepared.get("context"), dict):
        raise RSIRecoveryRequired("Prepared context is missing or invalid")
    if not isinstance(run, dict) or run.get("run_id") != prepared.get("run_id") or run.get("status") != "ready":
        raise RSIRecoveryRequired("Run does not match the ready prepared context")
    if not isinstance(run.get("snapshot"), dict):
        raise RSIRecoveryRequired("Run Memory snapshot is missing or invalid")
    frozen = deepcopy(run)
    frozen["recipe"] = deepcopy(prepared["recipe"])
    frozen["context"] = deepcopy(prepared["context"])
    return result(frozen)


def _delay(reply):
    try:
        delay = float(reply.get("retry_after_seconds", 2))
    except (TypeError, ValueError):
        delay = 2
    return min(max(delay, 0), 30)


async def prepare_ready_async(atoms, *, timeout=300, **params):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        reply = await atoms.prepare(**params)
        if reply.status == "ready" and reply.get("run_id"):
            return reply
        if reply.status != "processing":
            raise RSIRecoveryRequired(f"Unexpected CoSens prepare status: {reply.status}")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RSIRecoveryRequired("CoSens prepare did not become ready before timeout")
        await asyncio.sleep(min(_delay(reply), remaining))


def prepare_ready_sync(atoms, *, timeout=300, **params):
    deadline = time.monotonic() + timeout
    while True:
        reply = atoms.prepare(**params)
        if reply.status == "ready" and reply.get("run_id"):
            return reply
        if reply.status != "processing":
            raise RSIRecoveryRequired(f"Unexpected CoSens prepare status: {reply.status}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RSIRecoveryRequired("CoSens prepare did not become ready before timeout")
        time.sleep(min(_delay(reply), remaining))
