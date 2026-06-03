"""Auth endpoints — login / logout / me, proxied to foundation identity.

This module replaces the B-M1 placeholder that stored ``{user_id, tenant_id}``
in a plain ``base64(json)`` cookie — that scheme let any client edit the
cookie and impersonate another tenant.  The current flow:

* ``POST /auth/login`` calls foundation identity's ``POST /sessions`` (which
  internally talks to Keycloak), pulls ``access_token / refresh_token /
  user_id / tenant_id`` from the response, and writes them into a signed
  cookie via :class:`foundation_sdk.CookieSigner`.  The cookie is
  ``HttpOnly + SameSite=strict``; the access token never leaves the BFF and
  is forwarded to downstream services as the ``Authorization`` header.

* ``GET /auth/me`` re-resolves the caller's identity by calling foundation
  identity's ``GET /sessions/me`` with the access token.  This means the
  SPA never trusts client-side state — refreshing the page revalidates
  against the source of truth.

* ``POST /auth/logout`` clears the cookie and revokes the session at
  foundation identity (best-effort).
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from foundation_sdk import CookieSigner
from foundation_sdk.fastapi import get_tenant_id, get_user_id

from agent_bff.api._http import foundation_headers
from agent_bff.api.middleware import get_access_token
from agent_bff.errors import AuthenticationError

router = APIRouter(tags=["Auth"])
logger = logging.getLogger(__name__)


def _signer(request: Request) -> CookieSigner:
    return request.app.state.cookie_signer


async def _foundation_login(
    *, identity_url: str, email: str, password: str
) -> dict:
    """Call foundation identity to exchange credentials for a session.

    The contract is defined in ``saas-foundation/contracts/openapi/identity.yaml``
    under ``POST /sessions`` — the response carries ``access_token``,
    ``refresh_token``, ``user_id``, ``tenant_id`` and ``expires_at``.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{identity_url}/sessions",
            json={"method": "EMAIL_PASSWORD", "email": email, "password": password},
        )
    if resp.status_code == status.HTTP_201_CREATED:
        return resp.json()
    if resp.status_code in (401, 403):
        raise AuthenticationError("Invalid credentials")
    # 5xx / unexpected — surface as auth failure but log for ops.
    logger.warning(
        "identity login returned unexpected status",
        extra={"status": resp.status_code},
    )
    raise AuthenticationError("Identity service unavailable")


@router.post("/auth/login")
async def login(request: Request) -> Response:
    body = await request.json()
    email = body.get("email", "")
    password = body.get("password", "")
    if not email or not password:
        raise AuthenticationError("email and password required")

    settings = request.app.state.settings
    session = await _foundation_login(
        identity_url=settings.foundation_identity_url,
        email=email,
        password=password,
    )

    access_token = session.get("access_token", "")
    user_id = session.get("user_id", "")
    tenant_id = session.get("tenant_id", "")
    # The identity response also carries a refresh_token; we intentionally
    # do NOT persist it in the cookie.  Once refresh is implemented (M2) it
    # will live in a server-side refresh-token DB so a stolen cookie can't
    # extend a session on its own.
    if not (access_token and user_id and tenant_id):
        # Identity returned a 201 but the payload is malformed — refuse to
        # mint a cookie pointing nowhere.
        logger.error("identity returned 201 with missing fields")
        raise AuthenticationError("Identity response malformed")

    cookie_value = _signer(request).sign(
        {"user_id": user_id, "tenant_id": tenant_id, "access_token": access_token},
    )
    response = JSONResponse(
        content={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "email": email,
            "display_name": session.get("display_name", email),
        },
    )
    response.set_cookie(
        key=settings.cookie_name,
        value=cookie_value,
        max_age=settings.cookie_max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=204)
async def logout(request: Request) -> Response:
    settings = request.app.state.settings
    access_token = get_access_token()

    # Best-effort revoke at foundation; don't fail logout if identity is down
    # — the cookie is what binds the browser to the session, and we clear it
    # unconditionally below.
    if access_token:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                await client.delete(
                    f"{settings.foundation_identity_url}/sessions/current",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
        except httpx.HTTPError:
            logger.info("logout: identity revoke failed (cookie cleared anyway)")

    resp = Response(status_code=204)
    resp.delete_cookie(key=settings.cookie_name, path="/")
    return resp


@router.get("/auth/me")
async def me(request: Request) -> dict:
    """Resolve caller identity by re-asking foundation identity.

    The SPA hits this on mount to rehydrate state from the cookie.  We do
    *not* trust the cookie payload directly — instead we forward the
    embedded access token to foundation, which JWKS-verifies it and returns
    the authoritative profile.  That way a future SDK key rotation that
    revokes the JWT instantly invalidates the session even if the cookie is
    still nominally valid.
    """
    access_token = get_access_token()
    if not access_token:
        raise AuthenticationError("Not authenticated")

    settings = request.app.state.settings
    # Forward the same X-Tenant-Id / X-User-Id context every other
    # foundation call uses — otherwise the identity service's tenant
    # middleware 401s because it has no tenant context to bind to.
    headers = foundation_headers(
        tenant_id=get_tenant_id(),
        user_id=get_user_id(),
        access_token=access_token,
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/sessions/me",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("me: identity unreachable", extra={"exc": str(exc)})
        raise AuthenticationError("Identity service unavailable") from exc

    if resp.status_code == 401:
        raise AuthenticationError("Session expired")
    if resp.status_code >= 400:
        raise AuthenticationError("Identity rejected token")

    return resp.json()
