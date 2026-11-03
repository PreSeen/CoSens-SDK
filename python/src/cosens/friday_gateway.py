"""Construct OpenAI-compatible clients for the Friday US model gateway."""

FRIDAY_US_BASE_URL = "https://llm.cosens.ai/v1"


def friday_gateway_client(credential, *, auth_type="api_key", asynchronous=False, **options):
    """Use the user's Friday credential; pass the selected public model to each call.

    Call ``client.models.list()`` to obtain the account's public model IDs.
    """
    if not isinstance(credential, str) or not credential.strip() or any(c.isspace() for c in credential):
        raise ValueError("Friday credential must be a nonempty bare token")
    if auth_type not in ("api_key", "access_token"):
        raise ValueError("auth_type must be api_key or access_token")
    if any(key in options for key in ("api_key", "base_url", "default_headers", "max_retries")):
        raise ValueError("gateway credentials, base URL and authentication headers are fixed")
    from openai import AsyncOpenAI, OpenAI
    cls = AsyncOpenAI if asynchronous else OpenAI
    return cls(api_key=credential, base_url=FRIDAY_US_BASE_URL,
               default_headers={"X-Friday-Auth-Type": auth_type}, max_retries=0, **options)
