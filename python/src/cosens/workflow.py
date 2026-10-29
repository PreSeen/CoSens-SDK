"""Local orchestration helpers shared by transparent and explicit runs."""

import asyncio
import math
import time
from copy import deepcopy

from .errors import RSIRecoveryRequired
from .models import result


def _bounded_timeout(value):
    try:
        valid = not isinstance(value, bool) and value >= 0 and math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError("timeout must be a finite nonnegative number")
    return value


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


def freeze_recall_context(context):
    """Internal normalized adapter result, independent of the future HTTP schema."""
    if not isinstance(context, dict) or context.get("run_id"):
        raise RSIRecoveryRequired("Standalone recall must not create a Run")
    for field in ("recipe", "context", "snapshot"):
        if not isinstance(context.get(field), dict):
            raise RSIRecoveryRequired(f"Standalone recall {field} is missing or invalid")
    return result(deepcopy(context))


def _delay(reply):
    try:
        delay = float(reply.get("retry_after_seconds", 2))
    except (TypeError, ValueError, OverflowError):
        delay = 2
    if not math.isfinite(delay):
        delay = 2
    return min(max(delay, 0), 30)


async def prepare_ready_async(atoms, *, timeout=300, **params):
    deadline = asyncio.get_running_loop().time() + _bounded_timeout(timeout)
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
    deadline = time.monotonic() + _bounded_timeout(timeout)
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
