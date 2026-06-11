"""BFF proxy — feature flags (user-facing).

Serves the current user's flags with personal overrides merged, and
provides endpoints for users to manage their own flag preferences.

Upstream: foundation-feature-flag (RLS by X-Tenant-Id / X-User-Id).
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth

router = APIRouter(tags=["Feature Flags"])

_TIMEOUT = 5


async def _raise_upstream(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise HTTPException(status_code=resp.status_code, detail=detail)


# ---------------------------------------------------------------------------
# GET /flags — merged flags + user overrides
# ---------------------------------------------------------------------------


@router.get("/flags")
async def list_my_flags(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> list:
    """Return flags for the caller's tenant with user overrides merged."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    base = settings.foundation_feature_flag_url
    hdrs = foundation_headers(tenant_id, user_id, access_token)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            flags_coro = client.get(f"{base}/flags", headers=hdrs)
            overrides_coro = client.get(f"{base}/user-overrides", headers=hdrs)
            flags_resp, overrides_resp = await asyncio.gather(
                flags_coro, overrides_coro, return_exceptions=True,
            )

        flags = (
            flags_resp.json()
            if not isinstance(flags_resp, Exception) and flags_resp.status_code == 200
            else []
        )
        overrides_list = (
            overrides_resp.json()
            if not isinstance(overrides_resp, Exception) and overrides_resp.status_code == 200
            else []
        )

        override_map = {o["flag_key"]: o["enabled"] for o in overrides_list}

        for flag in flags:
            key = flag.get("key", "")
            if key in override_map and flag.get("user_overridable"):
                flag["user_override"] = override_map[key]
                flag["enabled"] = override_map[key]
            else:
                flag["user_override"] = None

        return flags
    except (httpx.ConnectError, httpx.TimeoutException):
        return []


# ---------------------------------------------------------------------------
# User flag overrides CRUD
# ---------------------------------------------------------------------------


@router.get("/user-flag-overrides")
async def list_user_overrides(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> list:
    """List the caller's personal flag overrides."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_feature_flag_url}/user-overrides",
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
        if resp.status_code != 200:
            return []
        return resp.json()
    except (httpx.ConnectError, httpx.TimeoutException):
        return []


@router.put("/user-flag-overrides/{key}")
async def set_user_override(
    key: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict:
    """Set a personal flag override."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    body = await request.json()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                f"{settings.foundation_feature_flag_url}/user-overrides/{key}",
                json=body,
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"feature-flag upstream unreachable: {exc}") from exc
    await _raise_upstream(resp)
    return resp.json()


@router.delete("/user-flag-overrides/{key}", status_code=204)
async def delete_user_override(
    key: str,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Remove a personal flag override (revert to tenant default)."""
    tenant_id, user_id, access_token = _auth
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_feature_flag_url}/user-overrides/{key}",
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(status_code=502, detail=f"feature-flag upstream unreachable: {exc}") from exc
    if resp.status_code >= 400:
        await _raise_upstream(resp)
    return Response(status_code=204)
