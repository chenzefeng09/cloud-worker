"""FastAPI dependency factory for feature-flag gates."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, Request, status

from feature_flag_client._client import get_ff_client

# Each service re-exports get_tenant_id from its own middleware module;
# the guard resolves it lazily at call time so the import only needs to
# exist in the service, not in this library.
_get_tenant_id: Callable[[], str] | None = None


def configure_tenant_resolver(fn: Callable[[], str]) -> None:
    """Register the function that returns the current request's tenant_id.

    Call once at app startup::

        from foundation_sdk.fastapi import get_tenant_id
        configure_tenant_resolver(get_tenant_id)
    """
    global _get_tenant_id
    _get_tenant_id = fn


def _resolve_tenant_id() -> str:
    if _get_tenant_id is None:
        raise RuntimeError(
            "feature_flag_client: call configure_tenant_resolver() at startup"
        )
    return _get_tenant_id()


def require_flag(key: str) -> Callable[..., Any]:
    """Return a FastAPI dependency that rejects if *key* is disabled.

    Usage::

        @router.get(
            "/sessions",
            dependencies=[Depends(require_flag("ai.thinking_ui"))],
        )
        async def list_sessions(...):
            ...
    """

    async def _guard(request: Request) -> None:
        tenant_id = _resolve_tenant_id()
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="feature-flag: tenant_id missing",
            )
        client = get_ff_client()
        if not await client.is_enabled(key, tenant_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"feature-flag: {key} is disabled",
            )

    return _guard
