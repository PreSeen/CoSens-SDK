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


def freeze_recall_context(context):
    """Internal normalized adapter result, independent of the future HTTP schema."""
    if not isinstance(context, dict):
        raise RSIRecoveryRequired("Standalone recall must not create a Run")
    for field in ("recipe", "context", "snapshot"):
        if not isinstance(context.get(field), dict):
            raise RSIRecoveryRequired(f"Standalone recall {field} is missing or invalid")
    return result(deepcopy(context))
