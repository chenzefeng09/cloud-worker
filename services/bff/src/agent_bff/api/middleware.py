"""BFF-specific middleware — signed session cookie parsing + service-token auth.

Request-id propagation and tenant/user header extraction live in the shared
``foundation_sdk.fastapi`` helpers; this module only owns the BFF-specific
piece: validating the signed session cookie and pushing its identity into the
shared identity contextvars (plus BFF-only contextvars for the OIDC
access_token that gets forwarded to foundation services as
``Authorization: Bearer``, the caller's clearance, and how the session
resolved).

Session cookie
--------------
The cookie is signed by :class:`foundation_sdk.CookieSigner` and carries
``{sid, tenant_id, user_id}``: ``sid`` names a server-side session whose
tokens :class:`agent_bff.api.web_session.WebSessionManager` resolves —
refreshing the access token first when it is about to expire.  Cookies minted
before server-side sessions (``access_token`` in the payload, no ``sid``) are
still honoured until they lapse; they cannot be refreshed.

A request that presents a session cookie is a browser request: when the cookie
does not authenticate (bad signature, expired, session ended or unavailable),
identity is cleared rather than left to the ``X-Tenant-Id`` / ``X-User-Id``
request headers, and :func:`get_session_state` tells ``require_auth`` whether
to answer 401 (sign in again) or 503 (retry).

Machine path (service-account)
-------------------------------
When the ``X-Service-Token`` header is present and equals the BFF's configured
``service_token`` (env ``BFF_SERVICE_TOKEN``, must be non-empty), the middleware
populates identity from ``X-Tenant-Id`` / ``X-User-Id`` headers and leaves the
access_token empty.  Constant-time comparison (``hmac.compare_digest``) prevents
timing attacks.

Downstream Bearer caveat
-------------------------
``foundation_headers`` in ``_http.py`` only sets ``Authorization: Bearer`` when
``access_token`` is non-empty; an empty string means the header is omitted.
Downstream services (app-registry, session-orchestrator) rely on
``X-Tenant-Id`` / ``X-User-Id`` for tenant-scoped operations in the shared dev
stack (they trust these headers directly rather than re-validating the JWT on
every internal hop).  If a downstream service is reconfigured to require a real
Bearer token on all routes, the ``BFF_SERVICE_TOKEN`` path will need a KC
client_credentials fallback to mint a short-lived token — flagged as a future
improvement.
"""

from __future__ import annotations

import hmac
import logging
from contextvars import ContextVar
from typing import Any

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

from agent_bff.api.web_session import SessionState, WebSessionManager
from agent_bff.domain.web_sessions import hash_session_id

logger = logging.getLogger(__name__)

# Re-export the shared getters so callers needn't know whether the data
# came from a header (other services) or a cookie (BFF).
__all__ = [
    "SessionMiddleware",
    "get_access_token",
    "get_clearance",
    "get_request_id",
    "get_session_state",
    "get_tenant_id",
    "get_user_id",
    "get_web_session_id",
]

_access_token_var: ContextVar[str] = ContextVar("agent_bff.access_token", default="")
_clearance_var: ContextVar[str] = ContextVar("agent_bff.clearance", default="")
_session_state_var: ContextVar[SessionState] = ContextVar(
    "agent_bff.session_state", default=SessionState.NONE
)
_web_session_var: ContextVar[str] = ContextVar("agent_bff.web_session", default="")


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


def get_session_state() -> SessionState:
    """How this request's session cookie resolved (``NONE`` without one)."""
    return _session_state_var.get()


def get_web_session_id() -> str:
    """Row id (``web_sessions.id``) of the session this request signed in with.

    ``""`` when the request did not authenticate with a server-side session
    (no cookie, a pre-server-side-session cookie, or a service call).
    """
    return _web_session_var.get()


class SessionMiddleware(BaseHTTPMiddleware):
    """Validate the signed session cookie and populate identity contextvars.

    Also handles the machine path: if ``X-Service-Token`` matches the
    configured secret (non-empty), identity is populated from the request
    headers ``X-Tenant-Id`` / ``X-User-Id`` without requiring a cookie.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_name: str,
        signer: CookieSigner,
        service_token: str = "",
        web_sessions: WebSessionManager | None = None,
    ) -> None:
        super().__init__(app)
        self._cookie_name = cookie_name
        self._signer = signer
        self._web_sessions = web_sessions
        # Store as bytes for constant-time compare; empty string = disabled.
        self._service_token_bytes: bytes = service_token.encode() if service_token else b""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # ── Machine path (service-account) ──────────────────────────────────
        # Must come BEFORE the cookie check so internal callers don't need a
        # session cookie.  Only active when service_token is non-empty.
        if self._service_token_bytes:
            incoming = request.headers.get("X-Service-Token", "")
            if incoming and hmac.compare_digest(
                self._service_token_bytes, incoming.encode()
            ):
                tenant_id = request.headers.get("X-Tenant-Id", "")
                user_id = request.headers.get("X-User-Id", "")
                push_identity(tenant_id=tenant_id, user_id=user_id)
                # access_token intentionally left empty — foundation_headers()
                # omits the Authorization header when access_token is "".
                _access_token_var.set("")
                _clearance_var.set("")
                logger.debug(
                    "service-token auth: tenant=%s user=%s", tenant_id, user_id
                )
                return await call_next(request)

        # ── Cookie path (human users) ────────────────────────────────────────
        raw = request.cookies.get(self._cookie_name)
        if raw:
            await self._authenticate_cookie(raw)

        return await call_next(request)

    async def _authenticate_cookie(self, raw: str) -> None:
        try:
            payload: dict[str, Any] = self._signer.unsign(raw)
        except SignatureExpiredError:
            logger.info("session cookie expired", extra={"cookie": self._cookie_name})
            _reject(SessionState.ENDED)
            return
        except SignatureInvalid:
            logger.warning(
                "session cookie signature invalid",
                extra={"cookie": self._cookie_name},
            )
            _reject(SessionState.NONE)
            return

        tenant_id = str(payload.get("tenant_id", ""))
        user_id = str(payload.get("user_id", ""))
        sid = payload.get("sid")
        if isinstance(sid, str) and sid:
            if self._web_sessions is None or not (tenant_id and user_id):
                _reject(SessionState.NONE)
                return
            resolution = await self._web_sessions.resolve(
                session_id=sid, tenant_id=tenant_id, user_id=user_id
            )
            if resolution.state is not SessionState.AUTHENTICATED:
                _reject(resolution.state)
                return
            access_token, clearance = resolution.access_token, resolution.clearance
            web_session_id = hash_session_id(sid)
        elif "access_token" in payload:
            # Pre server-side-session cookie: honoured until it lapses.
            access_token = str(payload.get("access_token", ""))
            clearance = str(payload.get("clearance", ""))
            web_session_id = ""
        else:
            _reject(SessionState.NONE)
            return

        push_identity(tenant_id=tenant_id, user_id=user_id)
        _access_token_var.set(access_token)
        _clearance_var.set(clearance)
        _web_session_var.set(web_session_id)
        _session_state_var.set(SessionState.AUTHENTICATED)


def _reject(state: SessionState) -> None:
    """The request's session cookie did not authenticate it."""
    push_identity(tenant_id="", user_id="")
    _access_token_var.set("")
    _clearance_var.set("")
    _web_session_var.set("")
    _session_state_var.set(state)
