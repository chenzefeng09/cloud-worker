"""BFF-specific middleware — signed session cookie parsing.

Request-id propagation and tenant/user header extraction live in the shared
``foundation_sdk.fastapi`` helpers; this module only owns the BFF-specific
piece: validating the signed session cookie and pushing its payload into the
shared identity contextvars (plus a BFF-only contextvar for the OIDC
access_token that gets forwarded to foundation services as
``Authorization: Bearer``).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from fastapi import Request, Response
from foundation_sdk import (
    CookieSigner,
    SignatureExpiredError,
    SignatureInvalid,
)
from foundation_sdk.fastapi import (
    get_request_id,
    get_tenant_id,
    get_user_id,
    push_identity,
)
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

# Re-export the shared getters so callers needn't know whether the data
# came from a header (other services) or a cookie (BFF).
__all__ = [
    "SessionMiddleware",
    "get_access_token",
    "get_clearance",
    "get_request_id",
    "get_tenant_id",
    "get_user_id",
]

_access_token_var: ContextVar[str] = ContextVar("agent_bff.access_token", default="")
_clearance_var: ContextVar[str] = ContextVar("agent_bff.clearance", default="")


def get_access_token() -> str:
    """Bearer token to forward to foundation services this request."""
    return _access_token_var.get()


def get_clearance() -> str:
    """User clearance (``L1``-``L5``) for this request, or ``""`` if unknown.

    Sourced from the signed session cookie payload (set at login from the
    access token's ``clearance`` claim); forwarded to session-orchestrator as
    ``X-User-Clearance`` on DLP-gated download proxies.
    """
    return _clearance_var.get()


class SessionMiddleware(BaseHTTPMiddleware):
    """Validate the signed session cookie and populate identity contextvars."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_name: str,
        signer: CookieSigner,
    ) -> None:
        super().__init__(app)
        self._cookie_name = cookie_name
        self._signer = signer

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw = request.cookies.get(self._cookie_name)
        if raw:
            try:
                payload = self._signer.unsign(raw)
            except SignatureExpiredError:
                logger.info("session cookie expired", extra={"cookie": self._cookie_name})
            except SignatureInvalid:
                logger.warning(
                    "session cookie signature invalid",
                    extra={"cookie": self._cookie_name},
                )
            else:
                push_identity(
                    tenant_id=str(payload.get("tenant_id", "")),
                    user_id=str(payload.get("user_id", "")),
                )
                _access_token_var.set(str(payload.get("access_token", "")))
                _clearance_var.set(str(payload.get("clearance", "")))

        return await call_next(request)
