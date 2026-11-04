"""Explicit business-model connection for server-side Recipe generation."""
import math
from urllib.parse import urlsplit


def model_connection_data(*, model_api=None):
    data = {}
    if model_api is None:
        return data, ()
    if not isinstance(model_api, dict) or set(model_api) - {"base_url", "model", "api_key", "temperature", "max_tokens", "friday_auth_type"}:
        raise ValueError("model_api requires base_url, model and optional api_key, temperature, max_tokens")
    url, model, key = (model_api.get(k, "") for k in ("base_url", "model", "api_key"))
    if not all(isinstance(v, str) for v in (url, model, key)) or not model.strip() or len(model) > 256:
        raise ValueError("model_api requires a model and string connection fields")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid or len(url) > 2048 or len(key) > 8192 or any(c in key for c in "\r\n"):
        raise ValueError("invalid model_api connection")
    gateway_url = url.strip().rstrip("/").removesuffix("/chat/completions")
    auth_type = model_api.get("friday_auth_type")
    if auth_type is not None and (not isinstance(auth_type, str) or auth_type not in {"api_key", "access_token"} or not key.strip() or gateway_url != "https://llm.cosens.ai/v1"):
        raise ValueError("Friday authentication requires a credential and the US gateway")
    if gateway_url == "https://llm.cosens.ai/v1" and auth_type is None:
        raise ValueError("Friday gateway requires an explicit friday_auth_type")
    temperature, tokens = model_api.get("temperature"), model_api.get("max_tokens")
    if temperature is not None and (type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("model_api.temperature must be between 0 and 2")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 131072):
        raise ValueError("model_api.max_tokens must be between 1 and 131072")
    data["model_api"] = dict(model_api)
    return data, (key,) if key else ()
