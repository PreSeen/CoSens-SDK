"""Private, allowlisted service endpoints. No public arbitrary base_url option."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Service:
    region: str
    url: str
    identity: str


_SERVICES = {
    'cn': Service('cn', 'https://friday-rsi-cn.stardust-ai.com/api', 'friday-rsi-cn.stardust-ai.com/api'),
    'us': Service('us', 'https://rsi.cosens.ai/api', 'rsi.cosens.ai/api'),
}
# Preserve the original CN identity so existing persisted state remains readable.
SERVICE_URL = _SERVICES['cn'].url
SERVICE_ID = _SERVICES['cn'].identity


def resolve_service():
    region = os.getenv('COSENS_REGION', 'cn').strip().lower()
    if region not in _SERVICES:
        raise ValueError('COSENS_REGION must be cn or us')
    return _SERVICES[region]
