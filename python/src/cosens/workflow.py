"""Local orchestration helpers shared by transparent and explicit runs."""

import asyncio
import time

from .errors import RSIRecoveryRequired


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
