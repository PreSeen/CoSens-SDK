import json
from types import SimpleNamespace

import pytest

from cosens.decision import parse_decision
from cosens.errors import RSIDecisionUnavailable


@pytest.mark.parametrize("value", [
    {"activate": True, "objective": "task", "task_family": "general"},
    {"activate": True, "objective": "task", "task_family": "general", "reason": "yes", "unauthorized": True},
])
def test_decision_parser_rejects_missing_or_extra_fields(value):
    with pytest.raises(RSIDecisionUnavailable):
        parse_decision(SimpleNamespace(output_text=json.dumps(value)))
