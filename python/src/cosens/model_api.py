"""Explicit business-model connection for server-side improvement jobs."""
import math
from urllib.parse import urlsplit


def improvement_data(*, reason=None, feedback=None, model_api=None):
    data = {}
    if reason is not None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValueError("reason must be nonempty text of at most 4000 characters")
        data["reason"] = reason
    if feedback is not None:
        if not isinstance(feedback, str) or not feedback.strip() or len(feedback) > 4000:
            raise ValueError("feedback must be nonempty text of at most 4000 characters")
        data["feedback"] = feedback
    if model_api is None:
        return data, ()
    if not isinstance(model_api, dict) or set(model_api) - {"base_url", "model", "api_key", "temperature", "max_tokens"}:
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
    temperature, tokens = model_api.get("temperature"), model_api.get("max_tokens")
    if temperature is not None and (type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("model_api.temperature must be between 0 and 2")
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= 131072):
        raise ValueError("model_api.max_tokens must be between 1 and 131072")
    data["model_api"] = dict(model_api)
    return data, (key,) if key else ()
