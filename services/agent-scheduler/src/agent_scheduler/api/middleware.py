"""Re-export shared request-context helpers.

This module used to define its own ``RequestIdMiddleware`` /
``TenantContextMiddleware`` / contextvars; all of that lives in
:mod:`foundation_sdk.fastapi` now.  We keep the thin re-export so callers
inside ``agent_scheduler`` don't have to learn the SDK import path.
"""

from foundation_sdk.fastapi import (
    get_request_id,
    get_tenant_id,
    get_user_id,
)

__all__ = ["get_request_id", "get_tenant_id", "get_user_id"]
