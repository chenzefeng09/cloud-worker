"""Lightweight async client for the foundation feature-flag service.

Typical setup (FastAPI)::

    from feature_flag_client import init_feature_flags

    # in create_app():
    init_feature_flags(settings.feature_flag_url)

Typical usage::

    from feature_flag_client import get_ff_client, require_flag

    # As a FastAPI dependency (route-level gate):
    @router.get("/foo", dependencies=[Depends(require_flag("ai.thinking_ui"))])
    async def foo(): ...

    # Imperative check inside business logic:
    client = get_ff_client()
    if await client.is_enabled("billing.saas_mode", tenant_id): ...
"""

from feature_flag_client._client import (
    FeatureFlagClient,
    get_ff_client,
    init_feature_flags,
)
from feature_flag_client._guard import require_flag

__all__ = [
    "FeatureFlagClient",
    "get_ff_client",
    "init_feature_flags",
    "require_flag",
]
