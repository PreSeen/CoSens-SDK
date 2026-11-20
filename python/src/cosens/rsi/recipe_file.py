"""Read and validate a local Recipe written in the Skill Markdown format."""

import re
from pathlib import Path

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .models import RecipeMetadata

_FRONTMATTER = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n((?:[^\n]*\n)*?)---[ \t]*(?:\r?\n|\Z)"
)


def read_recipe_file(path: str | Path):
    try:
        file = Path(path)
        if not file.is_file():
            raise OSError("Not a regular file")
        markdown = file.read_bytes().decode("utf-8")
    except (OSError, ValueError, TypeError):
        raise ValueError(
            "recipe_insert requires a readable UTF-8 Markdown file"
        ) from None

    header = _FRONTMATTER.match(markdown)
    if not header:
        raise ValueError("Recipe requires YAML frontmatter delimited by ---")

    try:
        metadata = YAML(typ="safe", pure=True).load(header[1])
        if not isinstance(metadata, dict):
            raise TypeError("Expected a mapping")
    except (YAMLError, TypeError):
        raise ValueError("Recipe frontmatter must be a valid YAML mapping") from None

    try:
        recipe = RecipeMetadata(
            name=metadata.get("name"), description=metadata.get("description")
        )
    except ValidationError:
        raise ValueError(
            "Recipe frontmatter requires nonempty string fields name and description"
        ) from None

    instructions = re.sub(r"\A\r?\n", "", markdown[header.end() :])
    return {**recipe.model_dump(), "instructions": instructions}, markdown
