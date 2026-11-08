"""The Recipe form stored in Memory: distilled text only, because evidence IDs mean nothing off this machine."""

import json

from jsonschema import Draft202012Validator, ValidationError

from .store import canonical_json

PUBLICATION_ID = "cosens-recipe/v1"
_TEXT = {"type": "string", "minLength": 1}
PUBLICATION_SCHEMA = {
    "type": "object",
    "required": ["schema", "recipe_id", "version", "recipe"],
    "properties": {
        "schema": {"const": PUBLICATION_ID},
        "recipe_id": _TEXT,
        "version": {"type": "integer", "minimum": 1},
        "recipe": {
            "type": "object", "additionalProperties": False, "required": ["title", "when", "when_not", "steps", "lessons"],
            "properties": {
                "title": _TEXT, "when": _TEXT, "when_not": _TEXT,
                "steps": {"type": "array", "minItems": 1, "items": {
                    "type": "object", "additionalProperties": False, "required": ["id", "instruction"],
                    "properties": {"id": _TEXT, "instruction": _TEXT}}},
                "lessons": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["when", "do", "why", "when_not"],
                    "properties": {"when": _TEXT, "do": _TEXT, "why": _TEXT, "when_not": _TEXT}}},
            },
        },
    },
}


def _without_evidence(items):
    return [{key: value for key, value in item.items() if key != "evidence_refs"} for item in items]


def publication_text(recipe_id, version, recipe):
    return canonical_json({
        "schema": PUBLICATION_ID, "recipe_id": recipe_id, "version": version,
        "recipe": {**recipe, "steps": _without_evidence(recipe["steps"]), "lessons": _without_evidence(recipe["lessons"])},
    })


def parse_publication(text):
    """Return the publication, or None for any other Memory text."""
    try:
        value = json.loads(text)
        Draft202012Validator(PUBLICATION_SCHEMA).validate(value)
    except (ValueError, TypeError, ValidationError):
        return None
    return value


async def publish_text(atoms, *, candidate_id, text, source_id, revision):
    return await atoms.memory_ingest(
        content=text, request_key=f"recipe-publish:{candidate_id}:{revision}", source_kind="recipe", durable=True,
        source_id=source_id, expected_revision=revision,
    )


async def recall_published(atoms, query, token_budget):
    recalled = await atoms.memory_recall(query=query, token_budget=token_budget)
    found = []
    for item in recalled.get("items", []):
        publication = parse_publication(item.get("text"))
        if publication is not None and isinstance(item.get("id"), str) and type(item.get("revision")) is int:
            found.append({"source_id": item["id"], "revision": item["revision"], "recipe": publication["recipe"],
                          "version": publication["version"], "score": item.get("score")})
    return found
