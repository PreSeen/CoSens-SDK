"""Private, allowlisted service endpoints. No public arbitrary base_url option."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    region: str
    url: str
    identity: str


_SERVICES = {
    'us': Service('us', 'https://rsi.cosens.ai/api', 'rsi.cosens.ai/api'),
}
SERVICE_URL = _SERVICES['us'].url
SERVICE_ID = _SERVICES['us'].identity


def resolve_service(region=None):
    region = os.getenv('COSENS_REGION', 'us') if region is None else region
    if not isinstance(region, str):
        raise ValueError('region must be us')
    region = region.strip().lower()
    if region not in _SERVICES:
        raise ValueError('COSENS_REGION must be us')
    return _SERVICES[region]


def resolve_api_key(api_key):
    if api_key is not None:
        return api_key
    return os.getenv('COSENS_API_KEY')
