"""BFF admin proxy — lifecycle (recycle-bin / sweep / legal-holds / approvals).

Proxy target: foundation lifecycle (``settings.foundation_lifecycle_url``).

All endpoints require super_admin (``require_super_admin_bff``).  A super_admin
acting on another tenant supplies the optional ``tenant_id`` query parameter to
override the forwarded ``X-Tenant-Id`` header.  Downstream is tenant-level and
receives X-Tenant-Id + X-User-Id.

Several upstream paths use the ``resource:action`` convention.  FastAPI/Starlette
match a literal ``:action`` suffix and still extract ``{bin_id}`` path params, so
the routes are declared directly.  httpx does not percent-encode the colon, so
``f"{base}/recycle-bin/{bin_id}:restore"`` reaches the upstream verbatim.

Hard-delete and legal-hold release take two people (lifecycle-v1 §4.4).  Upstream
they only open an approval request and answer 202, or 200 when the entry is
already destroyed / the hold already released, so these two routes relay the
upstream status and body unchanged.  A second admin carries the request out with
``/approval-requests/{approval_id}/approve``.  Requester and approver are both the
forwarded ``X-User-Id``; the upstream refuses the requester's own approval (403
``SELF_APPROVAL_FORBIDDEN``) and a decided or expired request (409
``APPROVAL_NOT_PENDING``), and ``_raise_for_upstream`` relays both with their
messages.

Upstream paths (foundation lifecycle):
    POST /recycle-bin:soft-delete
    POST /recycle-bin/{bin_id}:restore
    POST /recycle-bin/{bin_id}:hard-delete (202 approval request / 200 already destroyed)
    GET  /recycle-bin                      (query: cursor/limit)
    POST /sweep
    POST /legal-holds
    POST /legal-holds/{hold_id}:release    (202 approval request / 200 already released)
    GET  /legal-holds:check                (query: resource_type_id/resource_id)
    GET  /legal-holds                      (query: cursor/limit)
    GET  /approval-requests                (query: filter.status/filter.operation/cursor/limit)
    GET  /approval-requests/{approval_id}
    POST /approval-requests/{approval_id}/approve   (no body; approver from X-User-Id)
    POST /approval-requests/{approval_id}/reject    (no body; decider from X-User-Id)
    GET  /storage-quotas/admin/summary     (scope: storage.admin)
    PATCH /storage-quotas/admin/{tenant_id}/{bucket_purpose}
                                           (scope: storage.quota.write)
    GET  /orphan-reports                   (query: limit/before_id)
    GET  /orphan-reports/{report_id}
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from agent_bff.api._http import foundation_headers
from agent_bff.api.v1.admin import _raise_for_upstream, require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_SERVICE = "lifecycle"
_TIMEOUT = 15


def _headers(request: Request, auth: tuple) -> dict[str, str]:
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(target_tenant, user_id, access_token)


def _admin_scoped_headers(request: Request, auth: tuple) -> dict[str, str]:
    """Headers for cross-tenant *storage* admin calls.

    Attaches ``X-Foundation-Scopes: storage.admin storage.quota.write`` so
    the upstream (foundation-lifecycle) admits the request — the storage
    admin surface is scope-gated on top of the platform.admin authz check
    the BFF already enforces via require_super_admin_bff.
    """
    tenant_id, user_id, access_token = auth
    target_tenant = request.query_params.get("tenant_id") or tenant_id
    return foundation_headers(
        target_tenant, user_id, access_token, super_admin=True,
    )


def _forward_params(request: Request) -> dict[str, str]:
    """Forwarded query params minus the BFF-private ``tenant_id`` override."""
    return {k: v for k, v in request.query_params.items() if k != "tenant_id"}


async def _body(request: Request) -> dict:
    return await request.json() if await request.body() else {}


def _connect_502(exc: httpx.HTTPError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"{_SERVICE} upstream unreachable: {exc}",
    )


def _relay(resp: httpx.Response) -> JSONResponse:
    """The upstream's own status and body, so a 202 (approval request opened)
    does not reach the SPA as the 200 that means "already done"."""
    _raise_for_upstream(resp)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


# ── Recycle bin ─────────────────────────────────────────────────────────────────

@router.post("/admin/lifecycle/recycle-bin:soft-delete")
async def recycle_bin_soft_delete(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/recycle-bin:soft-delete",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "soft-deleted"}


@router.post("/admin/lifecycle/recycle-bin/{bin_id}:restore")
async def recycle_bin_restore(
    bin_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/recycle-bin/{bin_id}:restore",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "restored"}


@router.post("/admin/lifecycle/recycle-bin/{bin_id}:hard-delete")
async def recycle_bin_hard_delete(
    bin_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> JSONResponse:
    """Ask for the entry to be destroyed now: 202 with the approval request a
    second admin has to approve, or 200 if it is already destroyed."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/recycle-bin/{bin_id}:hard-delete",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    return _relay(resp)


@router.get("/admin/lifecycle/recycle-bin")
async def list_recycle_bin(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/recycle-bin",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Sweep ───────────────────────────────────────────────────────────────────────

@router.post("/admin/lifecycle/sweep")
async def sweep(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/sweep",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "swept"}


# ── Legal holds ─────────────────────────────────────────────────────────────────

@router.post("/admin/lifecycle/legal-holds", status_code=201)
async def create_legal_hold(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/legal-holds",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/lifecycle/legal-holds/{hold_id}:release")
async def release_legal_hold(
    hold_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> JSONResponse:
    """Ask for the hold to be released: 202 with the approval request a second
    admin has to approve, or 200 if it is already released."""
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/legal-holds/{hold_id}:release",
                json=body,
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    return _relay(resp)


@router.get("/admin/lifecycle/legal-holds:check")
async def check_legal_hold(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/legal-holds:check",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/lifecycle/legal-holds")
async def list_legal_holds(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/legal-holds",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Four-eyes approval requests ────────────────────────────────────────────────

@router.get("/admin/lifecycle/approval-requests")
async def list_approval_requests(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Newest first; ``filter.status`` / ``filter.operation`` / ``cursor`` /
    ``limit`` pass through."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/approval-requests",
                params=_forward_params(request),
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/lifecycle/approval-requests/{approval_id}")
async def get_approval_request(
    approval_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/approval-requests/{approval_id}",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/lifecycle/approval-requests/{approval_id}/approve")
async def approve_approval_request(
    approval_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Approve as the second admin, which carries the hard-delete or release out
    now.  The approver is the forwarded ``X-User-Id``, so no body is sent."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/approval-requests/{approval_id}/approve",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.post("/admin/lifecycle/approval-requests/{approval_id}/reject")
async def reject_approval_request(
    approval_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    """Reject (or, for the requester, withdraw) the request; nothing is destroyed
    or released.  The decider is the forwarded ``X-User-Id``."""
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_lifecycle_url}/approval-requests/{approval_id}/reject",
                headers=_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


# ── Storage quotas (cross-tenant admin) ────────────────────────────────────────
#
# These three surfaces are scope-gated upstream (storage.admin /
# storage.quota.write) — the BFF asserts those scopes via the super_admin
# branch of foundation_headers().  The downstream paths sit under
# /storage-quotas/admin/... (not the per-tenant /storage-quotas/{purpose}
# read endpoint, which is unrelated to this admin console).

@router.get("/admin/lifecycle/storage-quotas/summary")
async def storage_quota_summary(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/storage-quotas/admin/summary",
                params=_forward_params(request),
                headers=_admin_scoped_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.patch("/admin/lifecycle/storage-quotas/{tenant_id}/{bucket_purpose}")
async def patch_storage_quota(
    tenant_id: str,
    bucket_purpose: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    body = await _body(request)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{settings.foundation_lifecycle_url}"
                f"/storage-quotas/admin/{tenant_id}/{bucket_purpose}",
                json=body,
                headers=_admin_scoped_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json() if resp.content else {"status": "patched"}


# ── Orphan reconcile reports ───────────────────────────────────────────────────

@router.get("/admin/lifecycle/orphan-reports")
async def list_orphan_reports(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/orphan-reports",
                params=_forward_params(request),
                headers=_admin_scoped_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()


@router.get("/admin/lifecycle/orphan-reports/{report_id}")
async def get_orphan_report(
    report_id: str,
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.foundation_lifecycle_url}/orphan-reports/{report_id}",
                headers=_admin_scoped_headers(request, _auth),
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise _connect_502(exc) from exc
    _raise_for_upstream(resp)
    return resp.json()
