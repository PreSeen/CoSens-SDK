import json
from types import SimpleNamespace

import pytest

from cosens.decision import decide_async, decide_sync, parse_decision
from cosens.errors import RSIDecisionUnavailable


@pytest.mark.parametrize("value", [
    {"activate": True},
    {"activate": True, "reason": "yes", "objective": "the model no longer writes this"},
])
def test_decision_parser_rejects_missing_or_extra_fields(value):
    with pytest.raises(RSIDecisionUnavailable):
        parse_decision(SimpleNamespace(output_text=json.dumps(value)))


@pytest.mark.parametrize("limit", [None, 64])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.asyncio
async def test_decision_only_sends_output_limit_when_explicit(limit, asynchronous):
    calls = []
    decision = {"activate": False, "reason": "routine"}

    def create(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content=json.dumps(decision))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    async def async_create(**kwargs):
        return create(**kwargs)

    completions = SimpleNamespace(create=async_create if asynchronous else create)
    native = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    options = {} if limit is None else {"max_output_tokens": limit}
    result = await decide_async(native, "model", "task", [], **options) if asynchronous else decide_sync(native, "model", "task", [], **options)
    assert result == decision
    assert calls[0]["response_format"]["json_schema"]["name"] == "cosens_decision"
    if limit is None:
        assert "max_completion_tokens" not in calls[0]
    else:
        assert calls[0]["max_completion_tokens"] == limit
