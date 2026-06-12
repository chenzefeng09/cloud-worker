"""FastAPI dependencies — tenant context + DB session bound to that tenant.

Two invariants enforced here:

1. ``current_tenant`` refuses to fall back to query / body parameters when the
   ``X-Tenant-Id`` header is missing.  The header is injected by the API
   gateway from the verified JWT (``saas-foundation/docs/CONVENTIONS.md §3``);
   any path that allowed a query-string fallback would let a client
   self-declare a tenant, bypassing tenant isolation.

2. ``db_session`` depends on ``current_tenant`` / ``current_user`` so the
   SQLAlchemy session is *always* opened inside a transaction that has run
   ``set_config('app.tenant_id', ...)``.  Skipping that step makes RLS
   evaluate ``tenant_id = NULL`` and silently return zero rows; worse, an
   INSERT can succeed with whatever tenant_id the caller chose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_session_orchestrator.api.middleware import (
    get_clearance,
    get_tenant_id,
    get_user_id,
)
from agent_session_orchestrator.domain.db import get_session


async def current_tenant() -> str:
    """Resolve tenant id from the request-scoped contextvar.

    Returns 401 if the gateway did not inject ``X-Tenant-Id`` — that almost
    always means the caller is hitting the service directly without a valid
    bearer token, or the gateway is misconfigured.
    """
    tid = get_tenant_id()
    if not tid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Tenant-Id header required (must be injected by the API gateway)",
        )
    return tid


async def current_user() -> str:
    """Resolve user id from the contextvar; ``SYSTEM`` for unauthenticated jobs.

    Unlike tenant_id, missing user_id is non-fatal for some flows (cron tasks,
    health probes).  Endpoints that mutate user-owned data should re-check
    that ``current_user()`` is not ``"SYSTEM"`` themselves.
    """
    return get_user_id() or "SYSTEM"


async def current_clearance() -> str:
    """Resolve the caller's data clearance from the contextvar.

    The clearance rides in on the ``X-User-Clearance`` header (injected by the
    BFF on the download path) and is read into a contextvar by
    :class:`RequestContextMiddleware`.  When the header is absent the
    contextvar is an empty string, so we fall back to ``"L2"`` — a
    conservative non-privileged default that keeps DLP ``check:download``
    enforcing rather than silently elevating to a higher tier.
    """
    return get_clearance() or "L2"


async def db_session(
    tenant_id: str = Depends(current_tenant),
    user_id: str = Depends(current_user),
) -> AsyncIterator[AsyncSession]:
    """Yield a transaction-bound session with ``app.tenant_id`` already set.

    Implemented as ``async for`` over ``get_session`` so FastAPI's dependency
    cleanup correctly closes the underlying transaction on response.
    """
    async for session in get_session(tenant_id=tenant_id, user_id=user_id):
        yield session


TDbSession = Depends(db_session)
TTenantId = Depends(current_tenant)
TUserId = Depends(current_user)
TClearance = Depends(current_clearance)
