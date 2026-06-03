"""Fail-closed quota guard for agent-scheduler.

Calls the foundation billing service to enforce quota limits before
resource-consuming actions, and records usage after successful actions.

Fail-closed semantics
---------------------
If the billing service is unreachable, ``check`` raises HTTP 503 (Service
Unavailable) rather than allowing the request through.  This is intentional:
a broken billing service must not silently grant unlimited resources.

``record`` is best-effort — if it fails we log and continue, because rolling
back an already-completed sandbox spawn is not feasible.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import HTTPException, status

log = logging.getLogger(__name__)


class QuotaGuard:
    """Thin async wrapper around the billing quota REST API."""

    def __init__(self, billing_url: str, timeout: float = 5.0) -> None:
        # billing_url is the base URL, e.g. "http://billing:8007/api/billing/v1"
        self._billing_url = billing_url.rstrip("/")
        self._timeout = timeout

    async def check(
        self,
        tenant_id: str,
        dimension: str,
        requested: int = 1,
    ) -> None:
        """Assert tenant has quota for *requested* units of *dimension*.

        Raises
        ------
        HTTPException(503)  billing service unreachable (Fail-closed).
        HTTPException(502)  billing returned a non-200 response.
        HTTPException(402)  allowed=false — quota exhausted.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._billing_url}/quota/check",
                    json={"dimension": dimension, "requested": requested},
                    headers={"X-Tenant-Id": tenant_id},
                )
        except httpx.HTTPError as exc:
            log.error(
                "quota_guard.check: billing unreachable for tenant=%s dim=%s: %s",
                tenant_id, dimension, exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="billing service unavailable — request blocked (fail-closed)",
            ) from exc

        if resp.status_code != 200:
            log.error(
                "quota_guard.check: unexpected billing status %s for tenant=%s dim=%s",
                resp.status_code, tenant_id, dimension,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"billing service returned unexpected status {resp.status_code}",
            )

        data = resp.json()
        if not data.get("allowed", False):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "message": "quota exceeded",
                    "dimension": data.get("dimension", dimension),
                    "current": data.get("current"),
                    "limit": data.get("limit"),
                    "remaining": data.get("remaining", 0),
                },
            )

    async def record(
        self,
        tenant_id: str,
        dimension: str,
        amount: float = 1.0,
        description: str = "",
    ) -> None:
        """Record usage after a successful action.  Best-effort: never raises."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._billing_url}/quota/usage",
                    json={
                        "dimension": dimension,
                        "amount": amount,
                        "description": description,
                    },
                    headers={"X-Tenant-Id": tenant_id},
                )
            if resp.status_code not in (200, 201):
                log.warning(
                    "quota_guard.record: billing returned %s for tenant=%s dim=%s",
                    resp.status_code, tenant_id, dimension,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "quota_guard.record: failed for tenant=%s dim=%s: %s",
                tenant_id, dimension, exc,
            )
