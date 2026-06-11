"""BFF admin proxy — organisation structure (org-units / teams / members).

Proxy target: foundation-org (``settings.foundation_org_url``).

Every org endpoint is tenant-scoped (RLS by ``X-Tenant-Id``); the optional
``tenant_id`` query parameter overrides the forwarded tenant so a super_admin
can manage any tenant's structure.

Upstream paths (foundation-org):
    GET    /org-units                          → CursorPage[OrgUnit]
    POST   /org-units                          → 201 OrgUnit
    GET    /org-units/{ou_id}                  → OrgUnit
    PATCH  /org-units/{ou_id}                  → OrgUnit
    DELETE /org-units/{ou_id}                  → 204
    POST   /org-units/{ou_id}/move             → OrgUnit  (body: new_parent_id, reason)
    GET    /teams                              → CursorPage[Team]
    POST   /teams                              → 201 Team
    DELETE /teams/{team_id}                     → 204
    GET    /teams/{team_id}/members            → list[TeamMember]
    POST   /teams/{team_id}/members            → 201 TeamMember (body: user_id, role_in_team)
    DELETE /teams/{team_id}/members/{user_id}  → 204
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "org"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _params(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


# ── Org Units ────────────────────────────────────────────────────────────────

@router.get("/admin/org/org-units")
async def list_org_units(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_org_url}/org-units",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/org/org-units", status_code=201)
async def create_org_unit(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_org_url}/org-units",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/org/org-units/{ou_id}")
async def get_org_unit(
    ou_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_org_url}/org-units/{ou_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/org/org-units/{ou_id}")
async def update_org_unit(
    ou_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.foundation_org_url}/org-units/{ou_id}",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/org/org-units/{ou_id}")
async def delete_org_unit(
    ou_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Delete an org unit, archiving it first.

    foundation requires an org unit to be archived before it can be deleted
    (``delete`` rejects a live unit with NOT_EMPTY/reason=not_archived). The
    SPA only exposes a single "delete" action, so we run the two foundation
    calls — archive then delete — here, keeping the operation atomic from the
    SPA's point of view. ``archive`` is idempotent (returns the unit unchanged
    if already archived) and is the call that enforces the real emptiness
    checks, so a genuinely non-empty unit surfaces its NOT_EMPTY problem (with
    reason has_children / has_teams / has_members) at this step.
    """
    settings = request.app.state.settings
    headers = _headers(request, _auth)
    base = f"{settings.foundation_org_url}/org-units/{ou_id}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            archive_resp = await client.post(
                f"{base}/archive",
                json={"reason": "Archived automatically before deletion."},
                headers=headers,
            )
            _raise_for_upstream(archive_resp)
            resp = await client.delete(base, headers=headers)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


@router.post("/admin/org/org-units/{ou_id}/move")
async def move_org_unit(
    ou_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_org_url}/org-units/{ou_id}/move",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Teams ────────────────────────────────────────────────────────────────────

@router.get("/admin/org/teams")
async def list_teams(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_org_url}/teams",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/org/teams", status_code=201)
async def create_team(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_org_url}/teams",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/org/teams/{team_id}")
async def delete_team(
    team_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Delete a team.

    Unlike org-units, teams delete directly (no archive step). foundation
    rejects a team that still has active members with 409 TEAM_CODE_CONFLICT
    ("Team has active members; remove them first."); that problem is passed
    through unchanged so the SPA can prompt the operator to remove members.
    """
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_org_url}/teams/{team_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "deleted"}


@router.get("/admin/org/teams/{team_id}/members")
async def list_team_members(
    team_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> list | dict:
    """Upstream returns a *bare array* of TeamMember (not {items})."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_org_url}/teams/{team_id}/members",
                params=_params(request), headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/org/teams/{team_id}/members", status_code=201)
async def add_team_member(
    team_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_org_url}/teams/{team_id}/members",
                json=body, headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.delete("/admin/org/teams/{team_id}/members/{user_id}")
async def remove_team_member(
    team_id: str,
    user_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.delete(
                f"{settings.foundation_org_url}/teams/{team_id}/members/{user_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return {"status": "removed"}
