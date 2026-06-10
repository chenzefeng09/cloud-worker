"""BFF admin proxy — feature flags.

Proxy target: foundation-feature-flag (``settings.foundation_feature_flag_url``).

NOTE on spec vs. base reality (see delivery report):
  The spec listed ``GET /flags/{key}`` (read one flag).  The *running*
  foundation-feature-flag service (packages/feature-flag api/v1/flags.py) only
  implements ``GET /flags`` (list), ``PUT /flags/{flag}`` (upsert) and
  ``POST /flags/{flag}/kill-switch``.  The single-flag GET is therefore NOT
  shipped (listed as skipped in the report); the SPA reads a flag from the list.

Tenant scope: feature-flag is RLS-scoped by ``X-Tenant-Id``; pass the optional
``tenant_id`` query parameter to target another tenant.

Upstream paths:
    GET  /flags                    → list[FlagView]   (bare array)
    PUT  /flags/{flag}             → FlagView         (upsert)
    POST /flags/{flag}/kill-switch → KillSwitchResponse
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "feature-flag"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple, *, admin: bool = False) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    headers = foundation_headers(target_tenant, user_id, access_token)
    # Write endpoints (PUT / kill-switch) require ``X-Actor: ADMIN`` upstream.
    # Every caller has already passed ``require_super_admin_bff``, so the BFF
    # may safely assert an ADMIN actor for mutations. Reads stay unprivileged.
    if admin:
        headers["X-Actor"] = "ADMIN"
    return headers


def _params(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


@router.get("/admin/flags")
async def list_flags(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> list | dict:
    """Flag catalogue for the target tenant (upstream returns a bare array)."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_feature_flag_url}/flags",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.put("/admin/flags/{key}")
async def set_flag(
    key: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Upsert a flag's definition / rollout (upstream PUT /flags/{flag})."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.put(
                f"{settings.foundation_feature_flag_url}/flags/{key}",
                json=body, headers=_headers(request, _auth, admin=True),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/flags/{key}/kill-switch")
async def kill_flag(
    key: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Trip the kill-switch for a flag (upstream POST /flags/{flag}/kill-switch)."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_feature_flag_url}/flags/{key}/kill-switch",
                json=body, headers=_headers(request, _auth, admin=True),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
