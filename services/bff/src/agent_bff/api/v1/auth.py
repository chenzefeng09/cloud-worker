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
import secrets

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from foundation_sdk import (
    CookieSigner,
    SignatureExpiredError,
    SignatureInvalid,
)
from foundation_sdk.fastapi import get_tenant_id, get_user_id

from agent_bff.api._http import foundation_headers
from agent_bff.api._jwt import clearance_from_token
from agent_bff.api.dependencies import require_auth
from agent_bff.api.middleware import get_access_token
from agent_bff.errors import AuthenticationError, BFFError, ProxyError

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


async def _foundation_sso_login(
    *, identity_url: str, access_token: str, provider_id: str
) -> dict:
    """Exchange an SSO IdP token for a foundation session.

    Mirrors :func:`_foundation_login` but uses the ``SSO_TOKEN`` method of
    identity's ``POST /sessions``.  The response shape is identical to the
    EMAIL_PASSWORD flow.  The IdP ``access_token`` is sensitive — it is never
    logged.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{identity_url}/sessions",
            json={
                "method": "SSO_TOKEN",
                "access_token": access_token,
                "provider_id": provider_id,
            },
        )
    if resp.status_code == status.HTTP_201_CREATED:
        return resp.json()
    if resp.status_code in (401, 403):
        raise AuthenticationError("Invalid credentials")
    logger.warning(
        "identity sso login returned unexpected status",
        extra={"status": resp.status_code},
    )
    raise AuthenticationError("Identity service unavailable")


def _session_cookie_response(request: Request, *, session: dict, email: str) -> Response:
    """Mint the signed session cookie from an identity session payload.

    Shared by the EMAIL_PASSWORD and SSO_TOKEN login flows so both produce an
    identical cookie and response shape.
    """
    settings = request.app.state.settings
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

    # clearance lives in the access_token (Keycloak `clearance` claim), not the
    # identity SessionRead payload — decode it out and bake it into the cookie
    # so downstream requests don't have to re-parse the JWT every time.
    clearance = clearance_from_token(access_token)
    cookie_value = _signer(request).sign(
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "access_token": access_token,
            "clearance": clearance,
        },
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
    return _session_cookie_response(request, session=session, email=email)


@router.post("/auth/login/sso")
async def login_sso(request: Request) -> Response:
    """Exchange an SSO IdP ``access_token`` for a BFF session cookie.

    Body: ``{"access_token": str, "provider_id": str}``.  Forwarded to
    identity's ``POST /sessions`` with ``method=SSO_TOKEN``; the resulting
    session is written into the same signed cookie as EMAIL_PASSWORD login.
    """
    body = await request.json()
    access_token = body.get("access_token", "")
    provider_id = body.get("provider_id", "")
    if not access_token or not provider_id:
        raise AuthenticationError("access_token and provider_id required")

    settings = request.app.state.settings
    session = await _foundation_sso_login(
        identity_url=settings.foundation_identity_url,
        access_token=access_token,
        provider_id=provider_id,
    )
    return _session_cookie_response(
        request, session=session, email=session.get("email", "")
    )


# ── Browser SSO redirect flow ─────────────────────────────────────────────
#
# Unlike POST /auth/login/sso (SSO_TOKEN passthrough, where the SPA already
# holds an IdP token), this is the full browser-redirect Authorization-Code
# flow.  The browser is bounced to Keycloak, comes back to the BFF callback
# with a ?code=, and the BFF exchanges it server-side so the IdP code/token
# never touch the SPA.
#
# State is a signed payload carried through the round-trip.  It is signed
# with the same CookieSigner as the session cookie so Keycloak (and the
# browser) cannot forge or tamper with the provider/origin it carries.

# State payload keys are kept short to stay well under URL length limits.
_STATE_PROVIDER = "p"
_STATE_ORIGIN = "o"
_STATE_NONCE = "n"
# SSO redirect state is short-lived — the user just needs to bounce to the
# IdP and back, which takes seconds.  Cap it at 10 minutes so a leaked /
# replayed authorize URL can't be redeemed days later.
_STATE_MAX_AGE = 600


def _cors_origins(request: Request) -> list[str]:
    settings = request.app.state.settings
    return [o.strip() for o in settings.cors_origins.split(",") if o.strip()]


def _resolve_return_to(request: Request, return_to: str | None) -> str:
    """Validate ``return_to`` against the configured CORS origins.

    Missing → default to the first configured origin.  Present but not an
    exact match (scheme+host+port) → 400, since this value ends up in the
    redirect_uri we hand to Keycloak and is later trusted as the browser
    destination; an open redirect here would let an attacker bounce a
    freshly-minted session cookie to a site they control.
    """
    origins = _cors_origins(request)
    if not return_to:
        if not origins:
            raise BFFError(
                "no cors origins configured",
                code="AGENT.BFF.SSO_MISCONFIGURED",
                status=500,
            )
        return origins[0]
    if return_to not in origins:
        raise BFFError(
            "return_to is not an allowed origin",
            code="AGENT.BFF.SSO_BAD_RETURN_TO",
            status=400,
        )
    return return_to


def _fallback_origin(request: Request) -> str:
    origins = _cors_origins(request)
    return origins[0] if origins else ""


@router.get("/auth/sso/options")
async def sso_options(request: Request) -> Response:
    """PUBLIC — list the SSO login options the user can pick on the login page.

    Proxies foundation identity ``GET /sso/login-options``; upstream returns a
    bare array (its list-endpoint convention) which is normalised to
    ``{"items": [{"id", "name", "kind"}]}`` for the SPA.  No auth: the login
    page calls this before the user has a session.
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/sso/login-options",
            )
    except httpx.HTTPError as exc:
        logger.warning("sso/options: identity unreachable", extra={"exc": str(exc)})
        raise ProxyError("identity", "sso login-options unreachable") from exc

    body = resp.json()
    if isinstance(body, list):
        body = {"items": body}
    return JSONResponse(content=body, status_code=resp.status_code)


@router.get("/auth/sso/{provider_id}/start")
async def sso_start(request: Request, provider_id: str) -> Response:
    """PUBLIC — kick off the browser SSO redirect flow for ``provider_id``.

    Builds the per-request signed ``state``, asks identity for the IdP
    authorize URL (which already carries ``kc_idp_hint``), and 302-redirects
    the browser there.
    """
    settings = request.app.state.settings
    return_to = _resolve_return_to(request, request.query_params.get("return_to"))
    redirect_uri = f"{return_to}/api/bff/v1/auth/sso/callback"

    state = _signer(request).sign(
        {
            _STATE_PROVIDER: provider_id,
            _STATE_ORIGIN: return_to,
            _STATE_NONCE: secrets.token_hex(8),  # 16 hex chars
        },
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}"
                f"/sso/providers/{provider_id}/authorize-url",
                params={"redirect_uri": redirect_uri, "state": state},
            )
    except httpx.HTTPError as exc:
        logger.warning("sso/start: identity unreachable", extra={"exc": str(exc)})
        raise ProxyError("identity", "sso authorize-url unreachable") from exc

    if resp.status_code >= 400:
        logger.warning(
            "sso/start: identity rejected authorize-url request",
            extra={"status": resp.status_code, "provider_id": provider_id},
        )
        # Bounce the user back to the login page with a generic error rather
        # than surfacing a raw upstream error mid-redirect.
        return RedirectResponse(
            f"{return_to}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    authorize_url = resp.json().get("authorize_url", "")
    if not authorize_url:
        logger.error("sso/start: identity returned no authorize_url")
        return RedirectResponse(
            f"{return_to}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    return RedirectResponse(authorize_url, status_code=status.HTTP_302_FOUND)


@router.get("/auth/sso/callback")
async def sso_callback(request: Request) -> Response:
    """PUBLIC — Keycloak redirects the browser back here with ?code=&state=.

    Verifies the signed state, exchanges the code for a foundation session
    via identity ``POST /sessions`` (method ``SSO_CODE``), mints the same
    signed session cookie as the password login, and 302-redirects the
    browser to ``{origin}/home`` with the cookie attached.  Any failure
    bounces back to ``{origin}/login?sso_error=...`` — the IdP ``code`` and
    any tokens are never logged.
    """
    params = request.query_params
    raw_state = params.get("state", "")
    fallback = _fallback_origin(request)

    # Decode + verify state first so we know which origin to bounce errors to.
    try:
        state = _signer(request).unsign(raw_state, max_age_seconds=_STATE_MAX_AGE)
        origin = str(state.get(_STATE_ORIGIN, ""))
        provider_id = str(state.get(_STATE_PROVIDER, ""))
    except (SignatureInvalid, SignatureExpiredError):
        logger.warning("sso/callback: invalid or expired state")
        return RedirectResponse(
            f"{fallback}/login?sso_error=invalid_state",
            status_code=status.HTTP_302_FOUND,
        )
    if not origin or origin not in _cors_origins(request) or not provider_id:
        logger.warning("sso/callback: state missing origin/provider")
        return RedirectResponse(
            f"{fallback}/login?sso_error=invalid_state",
            status_code=status.HTTP_302_FOUND,
        )

    # Keycloak may redirect back with an error instead of a code.
    error = params.get("error")
    if error:
        # error is an OAuth error code (e.g. access_denied); safe to echo.
        logger.info("sso/callback: idp returned error", extra={"error": error})
        return RedirectResponse(
            f"{origin}/login?sso_error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    code = params.get("code", "")
    if not code:
        return RedirectResponse(
            f"{origin}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    # Reconstruct the SAME redirect_uri the authorize step used — identity /
    # Keycloak require it to match exactly for the code exchange.
    redirect_uri = f"{origin}/api/bff/v1/auth/sso/callback"
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/sessions",
                json={
                    "method": "SSO_CODE",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "provider_id": provider_id,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("sso/callback: identity unreachable", extra={"exc": str(exc)})
        return RedirectResponse(
            f"{origin}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    if resp.status_code != status.HTTP_201_CREATED:
        logger.warning(
            "sso/callback: identity rejected code exchange",
            extra={"status": resp.status_code},
        )
        return RedirectResponse(
            f"{origin}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    session = resp.json()
    access_token = session.get("access_token", "")
    user_id = session.get("user_id", "")
    tenant_id = session.get("tenant_id", "")
    if not (access_token and user_id and tenant_id):
        logger.error("sso/callback: identity returned 201 with missing fields")
        return RedirectResponse(
            f"{origin}/login?sso_error=login_failed",
            status_code=status.HTTP_302_FOUND,
        )

    # Mint the identical signed cookie the password / SSO_TOKEN paths produce,
    # but attach it to the redirect so the browser lands authenticated on /home.
    clearance = clearance_from_token(access_token)
    cookie_value = _signer(request).sign(
        {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "access_token": access_token,
            "clearance": clearance,
        },
    )
    response = RedirectResponse(
        f"{origin}/home",
        status_code=status.HTTP_302_FOUND,
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


@router.get("/auth/sessions")
async def list_auth_sessions(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> list:
    """Proxy to foundation identity GET /sessions — list all active login sessions
    for the current user.

    Response shape (list of ActiveSessionRead):
        [{
          "id": str,
          "ip": str | null,
          "user_agent": str | null,
          "device": str,          # parsed from user_agent by identity
          "last_seen_at": str | null,
          "issued_at": str,
          "current": bool,
        }, ...]

    Used by ProfilePage PF_SESSIONS block.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings

    headers = foundation_headers(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/sessions",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("auth/sessions: identity unreachable", extra={"exc": str(exc)})
        raise AuthenticationError("Identity service unavailable") from exc

    if resp.status_code == 401:
        raise AuthenticationError("Session expired")
    if resp.status_code == 404:
        # endpoint not yet implemented in this identity deployment — return empty
        return []
    if resp.status_code >= 400:
        raise AuthenticationError("Identity rejected request")

    data = resp.json()
    # identity may return a list directly or {"items": [...]}
    if isinstance(data, list):
        return data
    return data.get("items") or []


@router.get("/auth/mfa/methods")
async def list_mfa_methods(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> list:
    """Proxy to foundation identity GET /mfa/methods for the current user.

    Returns the list of active MFA methods.  The frontend uses this to
    decide whether to show "2FA enabled" or "not enabled".

    Response: list of MfaMethodRead objects:
        [{ "id": str, "kind": str, "label": str|null,
           "enrolled_at": datetime, "last_used_at": datetime|null }, ...]

    An empty list means no MFA methods are enrolled.
    """
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings

    headers = foundation_headers(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
    )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                f"{settings.foundation_identity_url}/mfa/methods",
                headers=headers,
            )
    except httpx.HTTPError as exc:
        logger.warning("mfa/methods: identity unreachable", extra={"exc": str(exc)})
        raise AuthenticationError("Identity service unavailable") from exc

    if resp.status_code == 401:
        raise AuthenticationError("Session expired")
    if resp.status_code >= 400:
        raise AuthenticationError("Identity rejected token")

    return resp.json()
