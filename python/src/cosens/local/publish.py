"""The Recipe form uploaded to the service: distilled text only, because evidence IDs mean nothing off this machine."""

import copy

from jsonschema import Draft202012Validator, ValidationError

from .recipe_stages import RECIPE_MAX_TEXT_CHARS, RECIPE_SCHEMA, output_problem, recipe_text_chars

# The generation schema without evidence; pinned in contract/local-rsi/recipe-upload.schema.json.
UPLOAD_SCHEMA = copy.deepcopy(RECIPE_SCHEMA)
for _field in ("steps", "lessons"):
    del UPLOAD_SCHEMA["properties"][_field]["items"]["properties"]["evidence_refs"]
    UPLOAD_SCHEMA["properties"][_field]["items"]["required"].remove("evidence_refs")


def without_evidence(recipe):
    def strip(items):
        return [{k: v for k, v in item.items() if k != "evidence_refs"} if isinstance(item, dict) else item for item in items]

    return {**recipe, **{field: strip(recipe[field]) for field in ("steps", "lessons") if isinstance(recipe.get(field), list)}}


def check_upload(recipe):
    """Raise ValueError unless the service would accept this Recipe."""
    try:
        Draft202012Validator(UPLOAD_SCHEMA).validate(recipe)
    except ValidationError as exc:
        raise ValueError("recipe does not match the upload schema: " + output_problem(exc)) from None
    step_ids = [step["id"] for step in recipe["steps"]]
    if len(step_ids) != len(set(step_ids)):
        raise ValueError("recipe step IDs must be unique")
    if recipe_text_chars(recipe) > RECIPE_MAX_TEXT_CHARS:
        raise ValueError(f"recipe text exceeds {RECIPE_MAX_TEXT_CHARS} characters")
    return recipe


def upload_form(recipe):
    return check_upload(without_evidence(recipe))
