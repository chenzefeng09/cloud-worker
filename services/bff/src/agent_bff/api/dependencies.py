"""BFF dependencies — tenant / user resolution and authentication gate."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, NoReturn

from fastapi import Depends

from agent_bff.api.middleware import (
    get_access_token,
    get_clearance,
    get_session_state,
    get_tenant_id,
    get_user_id,
)
from agent_bff.api.web_session import SessionState, SessionUnavailableError
from agent_bff.errors import AuthenticationError


async def current_tenant() -> str:
    return get_tenant_id()


async def current_user() -> str:
    return get_user_id()


async def current_access_token() -> str:
    """Bearer token to forward to downstream foundation services."""
    return get_access_token()


async def current_clearance() -> str:
    """User clearance (``L1``-``L5``) for this request, or ``""`` if unknown."""
    return get_clearance()


def raise_unauthenticated() -> NoReturn:
    """Refuse a request that carries no usable identity.

    401 tells the SPA to send the user to sign-in.  A session that exists but
    could not be resolved right now (session store or identity unreachable)
    is a 503 instead, so a transient outage does not sign anyone out.
    """
    state = get_session_state()
    if state is SessionState.UNAVAILABLE:
        raise SessionUnavailableError()
    if state is SessionState.ENDED:
        raise AuthenticationError("Session expired")
    raise AuthenticationError("Login required")


def require_auth() -> Callable[..., Coroutine[Any, Any, tuple[str, str, str]]]:
    """Dependency that requires authentication.

    Returns ``(tenant_id, user_id, access_token)``.  The third element is the
    OIDC access token of the caller's web session (refreshed by the session
    middleware before it expires); downstream services verify it via JWKS so
    the BFF doesn't need to be in the JWT trust chain.
    """

    async def _require_auth(
        tenant_id: str = Depends(current_tenant),
        user_id: str = Depends(current_user),
        access_token: str = Depends(current_access_token),
    ) -> tuple[str, str, str]:
        if not tenant_id or not user_id:
            raise_unauthenticated()
        return tenant_id, user_id, access_token

    return _require_auth


TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TAccessToken = Depends(current_access_token)
# Independent clearance dependency.  ``require_auth`` deliberately stays a
# 3-tuple — it has 40+ unpacking sites across the BFF — so routes that need the
# user's clearance opt in with this separate ``Depends`` instead of widening
# every call site.  Only the orchestrator DLP-download proxy uses it today.
TClearance = Depends(current_clearance)
