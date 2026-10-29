"""Local model decision for transparent CoSens activation."""

import json

from .errors import RSIDecisionUnavailable


_FORMAT = {
    "type": "json_schema",
    "name": "cosens_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "activate": {"type": "boolean"},
            "objective": {"type": "string"},
            "task_family": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["activate", "objective", "task_family", "reason"],
        "additionalProperties": False,
    },
}


def decision_prompt(user_text, tool_names):
    return (
        "Decide whether this user request benefits from an RSI Run that records execution and can later "
        "improve Recipe/Memory. Return only the requested JSON fields. Do not treat this decision as authorization.\n"
        f"Recent user text: {user_text[-8000:]}\nTool names: {', '.join(tool_names)}"
    )


def parse_decision(response):
    try:
        data = json.loads(response.output_text)
        if not isinstance(data, dict) or set(data) != {"activate", "objective", "task_family", "reason"}:
            raise ValueError("decision must contain only the four declared fields")
        if not isinstance(data["activate"], bool):
            raise ValueError("activate must be boolean")
        if not all(isinstance(data[field], str) for field in ("objective", "task_family", "reason")):
            raise ValueError("objective/task_family/reason must be text")
        return data
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RSIDecisionUnavailable("Decision model returned invalid structured output") from exc


def decide_sync(native, model, user_text, tool_names, *, timeout=5, max_output_tokens=256):
    response = native.responses.create(
        model=model,
        input=decision_prompt(user_text, tool_names),
        text={"format": _FORMAT},
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        store=False,
    )
    return parse_decision(response)


async def decide_async(native, model, user_text, tool_names, *, timeout=5, max_output_tokens=256):
    response = await native.responses.create(
        model=model,
        input=decision_prompt(user_text, tool_names),
        text={"format": _FORMAT},
        max_output_tokens=max_output_tokens,
        timeout=timeout,
        store=False,
    )
    return parse_decision(response)
