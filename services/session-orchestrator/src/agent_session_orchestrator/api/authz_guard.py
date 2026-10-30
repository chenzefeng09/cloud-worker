"""Fail-closed RBAC guard for session-orchestrator.

Factory ``require_permission(action, resource_type)`` returns a FastAPI
dependency that enforces the authz /check contract.  Four deny points —
all must pass or the request is rejected with HTTP 403:

1. user_id absent / SYSTEM / malformed → 403 (no network call)
2. authz /check network error (timeout, connection refused) → 403
3. authz returned non-200 status (422, 503, …) → 403
4. response body ``allowed != True`` → 403

``tenant_id`` is resolved from the request-scoped contextvar set by
``foundation_sdk.fastapi.install_request_context``; it is forwarded as the
``X-Tenant-Id`` header on the outbound check call (not in the body).

``require_identified_caller()`` keeps deny point 1 only.  It is for the
data-subject routes (privacy export / erasure / settings): those rights belong
to the person, not to a role, so no role may be required to use them on one's
own data.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import HTTPException, Request, status

from agent_session_orchestrator.api.middleware import get_tenant_id, get_user_id

_USER_ID_RE = re.compile(r"^usr_[0-9A-Z]{26}$")

_AUTHZ_TIMEOUT = 2.0  # seconds — tight; fail-closed on timeout


def _require_identity() -> tuple[str, str]:
    """Deny point 1: a tenant and a real, well-formed user id (no network)."""
    tenant_id = get_tenant_id()
    user_id = get_user_id()

    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="RBAC: tenant_id missing",
        )
    if not user_id or user_id == "SYSTEM":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="RBAC: user_id missing or SYSTEM",
        )
    if not _USER_ID_RE.match(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="RBAC: user_id format invalid",
        )
    return tenant_id, user_id


def require_identified_caller() -> Callable[..., Any]:
    """Return a dependency that admits any identified user, with no authz check.

    For routes a caller uses on their *own* data whatever their role: the GDPR
    data-subject rights (access, erasure, restriction, objection).  Gating them
    on a role permission would leave whoever lacks it unable to exercise a
    right the law gives them — the ``member`` role, for one, holds no
    ``session.delete``.  Keeping each caller to their own data is the route's
    job; an ``X-Actor: ADMIN`` caller passes as in ``require_permission``.
    """

    async def _guard(request: Request) -> None:
        if request.headers.get("X-Actor") == "ADMIN":
            return
        _require_identity()

    return _guard


def require_permission(action: str, resource_type: str) -> Callable[..., Any]:
    """Return a FastAPI dependency that enforces ``action`` on ``resource_type``.

    Usage::

        @router.get(
            "/sessions",
            dependencies=[Depends(require_permission("session.read", "session"))],
        )
        async def list_sessions(...):
            ...
    """

    async def _guard(request: Request) -> None:
        # ── Admin bypass: BFF already enforced platform.admin ───────────────
        if request.headers.get("X-Actor") == "ADMIN":
            return

        # ── Deny point 1: identity checks (no network) ──────────────────────
        tenant_id, user_id = _require_identity()

        # ── Deny point 2: authz network call ────────────────────────────────
        settings = request.app.state.settings
        authz_url: str = settings.authz_url

        try:
            async with httpx.AsyncClient(timeout=_AUTHZ_TIMEOUT) as client:
                resp = await client.post(
                    f"{authz_url}/check",
                    json={
                        "user_id": user_id,
                        "action": action,
                        "resource_type": resource_type,
                    },
                    headers={"X-Tenant-Id": tenant_id},
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="RBAC: authz service unreachable",
            ) from exc

        # ── Deny point 3: non-200 response ──────────────────────────────────
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"RBAC: authz returned HTTP {resp.status_code}",
            )

        # ── Deny point 4: allowed check ─────────────────────────────────────
        try:
            body = resp.json()
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="RBAC: authz response unparseable",
            ) from None

        if body.get("allowed") is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="RBAC: permission denied",
            )

    return _guard
