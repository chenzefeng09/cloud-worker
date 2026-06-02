"""BFF dependencies — tenant / user resolution and authentication gate."""

from __future__ import annotations

from fastapi import Depends

from agent_bff.api.middleware import get_access_token, get_tenant_id, get_user_id
from agent_bff.errors import AuthenticationError


async def current_tenant() -> str:
    return get_tenant_id()


async def current_user() -> str:
    return get_user_id()


async def current_access_token() -> str:
    """Bearer token to forward to downstream foundation services."""
    return get_access_token()


def require_auth():
    """Dependency that requires authentication.

    Returns ``(tenant_id, user_id, access_token)``.  The third element is
    the OIDC access token retrieved at login; downstream services verify
    it via JWKS so the BFF doesn't need to be in the JWT trust chain.
    """

    async def _require_auth(
        tenant_id: str = Depends(current_tenant),
        user_id: str = Depends(current_user),
        access_token: str = Depends(current_access_token),
    ) -> tuple[str, str, str]:
        if not tenant_id or not user_id:
            raise AuthenticationError("Login required")
        return tenant_id, user_id, access_token

    return _require_auth


TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TAccessToken = Depends(current_access_token)
