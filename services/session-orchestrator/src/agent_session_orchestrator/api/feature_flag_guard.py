"""Feature-flag guard — re-exports from the shared library.

This thin wrapper configures the tenant resolver at import time and
re-exports the public API so existing ``from ...feature_flag_guard import``
paths keep working.
"""

from feature_flag_client import (
    FeatureFlagClient,
    get_ff_client,
    init_feature_flags,
    require_flag,
)
from feature_flag_client._guard import configure_tenant_resolver

from agent_session_orchestrator.api.middleware import get_tenant_id

configure_tenant_resolver(get_tenant_id)

__all__ = [
    "FeatureFlagClient",
    "get_ff_client",
    "init_feature_flags",
    "require_flag",
]
