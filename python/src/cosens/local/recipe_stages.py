"""Recipe attribution and generation: prompts, schemas, validation and the feedback gate. No IO."""

import json
from dataclasses import dataclass
from typing import NamedTuple, Optional

from jsonschema import Draft202012Validator, ValidationError

EVIDENCE_REF = {
    "anyOf": [
        {"type": "object", "additionalProperties": False, "required": ["revision_id", "event_id"],
         "properties": {"revision_id": {"type": "string"}, "event_id": {"type": "string"}}},
        {"type": "object", "additionalProperties": False, "required": ["memory_item_id"],
         "properties": {"memory_item_id": {"type": "string", "minLength": 1}}},
        {"type": "object", "additionalProperties": False, "required": ["artifact_id"],
         "properties": {"artifact_id": {"type": "string", "minLength": 1}}},
        {"type": "object", "additionalProperties": False, "required": ["feedback_id"],
         "properties": {"feedback_id": {"type": "string", "minLength": 1}}},
    ]
}
_EVIDENCE_REFS = {"type": "array", "minItems": 1, "maxItems": 20, "items": EVIDENCE_REF}

RECIPE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "when", "when_not", "steps", "lessons"],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 300},
        "when": {"type": "string", "minLength": 1, "maxLength": 600},
        "when_not": {"type": "string", "minLength": 1, "maxLength": 600},
        "steps": {
            "type": "array", "minItems": 1, "maxItems": 8,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "instruction", "evidence_refs"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 100},
                    "instruction": {"type": "string", "minLength": 1, "maxLength": 600},
                    "evidence_refs": _EVIDENCE_REFS,
                },
            },
        },
        "lessons": {
            "type": "array", "maxItems": 5,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["when", "do", "why", "when_not", "evidence_refs"],
                "properties": {
                    "when": {"type": "string", "minLength": 1, "maxLength": 400},
                    "do": {"type": "string", "minLength": 1, "maxLength": 400},
                    "why": {"type": "string", "minLength": 1, "maxLength": 400},
                    "when_not": {"type": "string", "minLength": 1, "maxLength": 400},
                    "evidence_refs": _EVIDENCE_REFS,
                },
            },
        },
    },
}

# Every field is required because structured-output providers reject optional properties.
ATTRIBUTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["actionable", "limitations", "possible_causes"],
    "properties": {
        "actionable": {"type": "boolean"},
        "limitations": {"type": "string", "maxLength": 4000},
        "possible_causes": {
            "type": "array", "maxItems": 10,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["reason", "uncertainty", "evidence_refs"],
                "properties": {
                    "reason": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "uncertainty": {"type": "string", "maxLength": 4000},
                    "evidence_refs": _EVIDENCE_REFS,
                },
            },
        },
    },
}

RECIPE_MAX_TEXT_CHARS = 6000
MODEL_OUTPUT_ATTEMPTS = 3
MAX_HISTORY_REVISIONS = 30
MAX_CONTEXT_BYTES = 4_000_000
NO_ACTIONABLE_FEEDBACK = "no_actionable_feedback"

MEMORY_TOKEN_BUDGET = 2000
MEMORY_MIN_SCORE = 0.35
MEMORY_RELATIVE_SCORE = 0.6
MEMORY_MAX_ITEMS = 8
MEMORY_DUPLICATE_SIMILARITY = 0.85

ATTRIBUTION_INSTRUCTION = (
    "Analyze recorded feedback against inputs, actual events and before/after artifacts. Return possible causes with "
    "evidence and uncertainty. Feedback is a claim, not proof; each feedback records its source (human, evaluator, "
    "reference, automated_check or downstream), so weigh it by where it comes from. Use only supplied artifact, event, "
    "Memory or feedback references. Do not invent a failure when none is supported. "
    "Set actionable to false when the feedback only thanks, approves or acknowledges the result and asks for no change; "
    "set it to true when it corrects the result, adds a constraint or preference, or reports a failure."
)
GENERATION_INSTRUCTION = (
    "Extract a reusable procedure from the recorded task history and the supplied attribution. "
    "The host explicitly requested generation after external confirmation; this is not independent validation. "
    "Cite supplied event, artifact, Memory or feedback references for every step and lesson. "
    "An artifact proves content, not that an action occurred. Do not treat feedback or a possible cause as proven fact. "
    "Keep case-specific names and answers in evidence only. Describe applicability, exclusions and checks. "
    "Keep the Recipe proportional to the task: a simple task needs at most 3 steps and no lessons. "
    "Every lesson must come from recorded feedback or an observed failure. Do not add steps or lessons about "
    "Memory screening, artifact persistence, feedback classification, evidence handling or other process "
    "meta-rules unless the recorded feedback explicitly asks for them."
)
ACKNOWLEDGED_ONLY_INSTRUCTION = " The feedback only acknowledged the result; do not derive lessons from it."
IMPROVE_INSTRUCTION = (
    " Update the supplied base_recipe using the new execution, feedback and attribution. "
    "Preserve unaffected valid steps and lessons; do not re-distill from scratch or require a separately selected cause."
)


class ModelOutputInvalid(ValueError):
    """The model kept returning output that fails the stage's schema or evidence checks."""


class Rejection(NamedTuple):
    kind: str
    message: str


@dataclass(frozen=True)
class EvidenceIndex:
    events: frozenset
    artifacts: frozenset
    memories: frozenset
    feedback: frozenset


@dataclass(frozen=True)
class StageResult:
    analysis: dict
    models: dict
    recipe: Optional[dict]  # None: the feedback gate kept the previous version


def evidence_index(context):
    history = context["history"]
    return EvidenceIndex(
        events=frozenset((h["revision_id"], e["id"]) for h in history for e in h["events"]),
        artifacts=frozenset(a["artifact_id"] for h in history for a in h["materials"] + h["outputs"]),
        memories=frozenset(item["id"] for item in context["memory"].get("items", [])),
        feedback=frozenset(f["feedback_id"] for h in history for f in h.get("feedback", [])),
    )


def valid_evidence(ref, index):
    if "artifact_id" in ref:
        return ref["artifact_id"] in index.artifacts
    if "memory_item_id" in ref:
        return ref["memory_item_id"] in index.memories
    if "feedback_id" in ref:
        return ref["feedback_id"] in index.feedback
    return (ref.get("revision_id"), ref.get("event_id")) in index.events


def invalid_refs(items, index):
    return [ref for item in items for ref in item["evidence_refs"] if not valid_evidence(ref, index)]


def recipe_text_chars(recipe):
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "evidence_refs"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return len(json.dumps(strip(recipe), ensure_ascii=False))


def attribution_rejection(value, index):
    invalid = invalid_refs(value["possible_causes"], index)
    if not invalid:
        return None
    return Rejection("evidence", "it cited references that are not in this context; copy every reference exactly from "
                                 "the supplied context, not from base_recipe. Invalid references: "
                     + json.dumps(invalid, ensure_ascii=False))


def recipe_rejection(value, index):
    invalid = invalid_refs(value["steps"] + value["lessons"], index)
    if invalid:
        return Rejection("evidence", "it failed evidence validation. Copy every reference exactly from the supplied "
                                     "context. Invalid references: " + json.dumps(invalid, ensure_ascii=False))
    step_ids = [step["id"] for step in value["steps"]]
    if len(step_ids) != len(set(step_ids)):
        return Rejection("duplicate_step", "it failed evidence validation. Use unique step IDs.")
    size = recipe_text_chars(value)
    if size > RECIPE_MAX_TEXT_CHARS:
        return Rejection("too_long", f"it is {size} characters without evidence references; shorten it to at most "
                                     f"{RECIPE_MAX_TEXT_CHARS} by merging steps and dropping generic advice")
    return None


def classify_output(stage, value, context):
    """Return why an output is rejected (schema, evidence, duplicate_step, too_long), or None when accepted."""
    schema, rejection = {"recipe": (RECIPE_SCHEMA, recipe_rejection),
                         "attribution": (ATTRIBUTION_SCHEMA, attribution_rejection)}[stage]
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError:
        return "schema"
    rejected = rejection(value, evidence_index(context))
    return rejected.kind if rejected else None


def output_problem(exc):
    """Describe a rejected response for the retry instruction, without provider text."""
    if isinstance(exc, json.JSONDecodeError):
        return "the final response was not valid JSON"
    if isinstance(exc, ValidationError):
        path = "/" + "/".join(str(part) for part in exc.absolute_path)
        return f"schema violation at {path}: {exc.message[:300]}"
    return str(exc)[:300]


async def validated_output(model, instruction, schema, payload, rejection):
    """Retry a stage whose output fails parsing, schema or evidence checks, saying why."""
    for attempt in range(MODEL_OUTPUT_ATTEMPTS):
        try:
            value = await model.call(instruction=instruction, schema=schema, context=payload)
            Draft202012Validator(schema).validate(value)
            rejected = rejection(value)
            problem = rejected.message if rejected else None
        except (ValueError, ValidationError) as exc:
            problem = output_problem(exc)
        if problem is None:
            return value
        if attempt == MODEL_OUTPUT_ATTEMPTS - 1:
            raise ModelOutputInvalid(problem)
        instruction += " Previous response was rejected: " + problem


async def run_recipe_stages(model, context, parent, *, on_phase):
    """Attribute feedback when there is any, apply the gate, and generate the next Recipe."""
    index = evidence_index(context)
    analysis = {"possible_causes": [], "limitations": "No corrective feedback was recorded."}
    models = {"attribution": None, "generation": None}
    if index.feedback:
        on_phase("analyzing")
        analysis = await validated_output(
            model, ATTRIBUTION_INSTRUCTION, ATTRIBUTION_SCHEMA,
            {"role": "workflow_recipe_attribution", "context": context, "base_recipe": parent},
            lambda value: attribution_rejection(value, index),
        )
        models["attribution"] = model.identity
    if parent and analysis.get("actionable") is False:
        return StageResult(analysis, models, None)
    on_phase("generating")
    instruction = GENERATION_INSTRUCTION
    if analysis.get("actionable") is False:
        instruction += ACKNOWLEDGED_ONLY_INSTRUCTION
    if parent:
        instruction += IMPROVE_INSTRUCTION
    recipe = await validated_output(
        model, instruction, RECIPE_SCHEMA,
        {"role": "workflow_recipe_improvement" if parent else "workflow_recipe_distillation",
         "context": context, "base_recipe": parent, "attribution": analysis},
        lambda value: recipe_rejection(value, index),
    )
    models["generation"] = model.identity
    return StageResult(analysis, models, recipe)


def _has_source_refs(value):
    if isinstance(value, dict):
        return {"source_id", "revision", "content_hash", "start", "end"} <= value.keys() or any(
            _has_source_refs(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_source_refs(v) for v in value)
    return False


def _near_duplicate(a, b):
    union = a | b
    return bool(union) and len(a & b) / len(union) >= MEMORY_DUPLICATE_SIMILARITY


def memory_snapshot(items):
    """Freeze strong, distinct, source-backed recall hits; items without a score cannot be ranked and stay."""
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("invalid Memory recall response")
    grounded = [item for item in items if isinstance(item.get("id"), str) and item["id"] and _has_source_refs(item)]
    if len({item["id"] for item in grounded}) != len(grounded):
        raise ValueError("duplicate Memory item IDs")
    scores = [item["score"] for item in grounded if isinstance(item.get("score"), (int, float))]
    floor = max(MEMORY_MIN_SCORE, max(scores, default=0) * MEMORY_RELATIVE_SCORE)
    kept, seen, low, duplicate = [], [], 0, 0
    for item in grounded:
        score = item.get("score")
        if isinstance(score, (int, float)) and score < floor:
            low += 1
            continue
        words = set(str(item.get("text", "")).lower().split())
        if any(_near_duplicate(words, other) for other in seen):
            duplicate += 1
            continue
        if len(kept) == MEMORY_MAX_ITEMS:
            low += 1
            continue
        kept.append(item)
        seen.append(words)
    return {"status": "captured", "items": kept, "omitted_unreferenced": len(items) - len(grounded),
            "omitted_low_relevance": low, "omitted_duplicate": duplicate}
