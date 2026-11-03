import json
from types import SimpleNamespace

import pytest

from cosens.decision import decide_async, decide_sync, parse_decision
from cosens.errors import RSIDecisionUnavailable


@pytest.mark.parametrize("value", [
    {"activate": True, "objective": "task", "task_family": "general"},
    {"activate": True, "objective": "task", "task_family": "general", "reason": "yes", "unauthorized": True},
])
def test_decision_parser_rejects_missing_or_extra_fields(value):
    with pytest.raises(RSIDecisionUnavailable):
        parse_decision(SimpleNamespace(output_text=json.dumps(value)))


@pytest.mark.parametrize("limit", [None, 64])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_decision_only_sends_output_limit_when_explicit(limit, asynchronous):
    calls = []
    decision = {"activate": False, "objective": "task", "task_family": "general", "reason": "routine"}

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(output_text=json.dumps(decision))

    async def async_create(**kwargs):
        return create(**kwargs)

    native = SimpleNamespace(responses=SimpleNamespace(create=async_create if asynchronous else create))
    options = {} if limit is None else {"max_output_tokens": limit}
    result = await decide_async(native, "model", "task", [], **options) if asynchronous else decide_sync(native, "model", "task", [], **options)
    assert result == decision
    if limit is None:
        assert "max_output_tokens" not in calls[0]
    else:
        assert calls[0]["max_output_tokens"] == limit
