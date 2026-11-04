"""A regional model client using the same Friday API Key as CoSens."""
from ._config import resolve_api_key, resolve_service

FRIDAY_US_BASE_URL = "https://llm.cosens.ai/v1"
_GATEWAYS = {"cn": "https://friday-llm-gateway.corpintra.rosettalab.top/v1", "us": FRIDAY_US_BASE_URL}


def friday_gateway_client(api_key=None, *, region=None, asynchronous=False, **options):
    service = resolve_service(region)
    key = resolve_api_key(api_key)
    if not isinstance(key, str) or not key or any(c.isspace() for c in key):
        raise ValueError("Set COSENS_API_KEY or pass a bare Friday user API Key")
    if any(name in options for name in ("auth_type", "base_url", "default_headers", "max_retries")):
        raise ValueError("gateway URL and API Key authentication are selected automatically")
    from openai import AsyncOpenAI, OpenAI
    cls = AsyncOpenAI if asynchronous else OpenAI
    return cls(api_key=key, base_url=_GATEWAYS[service.region], default_headers={"X-Friday-Auth-Type": "api_key"}, max_retries=0, **options)
