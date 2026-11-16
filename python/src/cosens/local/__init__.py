"""Local RSI: requires ``pip install 'cosens-sdk[local]'``."""

try:
    from .codex_cli import CodexCLIModel
    from .engine import LocalRSI
    from .models import ChatCompletionsModel, ModelUnavailable, RecipeModel
    from .web_search import WebSearch, WebSearchUnavailable
except ModuleNotFoundError as error:
    if (error.name or "").split(".")[0] != "jsonschema":
        raise
    raise ImportError("cosens.local needs the 'local' extra: pip install 'cosens-sdk[local]'") from error

__all__ = ["ChatCompletionsModel", "CodexCLIModel", "LocalRSI", "ModelUnavailable", "RecipeModel", "WebSearch",
           "WebSearchUnavailable"]
