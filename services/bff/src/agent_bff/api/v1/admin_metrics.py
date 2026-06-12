"""BFF admin proxy — Prometheus range queries for the storage admin console.

This is a thin pass-through for the small slice of Prometheus the storage
admin pages need (used-bytes time series per tenant / bucket purpose).  It
exists so the browser does not need cross-origin access to Prometheus and
so we can gate the query behind the BFF's super_admin guard.

Only one upstream is supported today:
    GET /api/v1/query_range   (Prometheus range vector)

Request shape (BFF):
    GET /admin/metrics/storage-quotas/series
        ?range=24h                  human-friendly window (parsed)
        &step=5m                    Prometheus step
        &purpose=workspace          optional label filter
        &tenant_id=tnt_xxx          optional label filter

Response shape: the upstream Prometheus body is returned verbatim::
    {"status": "success", "data": {"resultType": "matrix", "result": [...]}}
"""

from __future__ import annotations

import re
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from agent_bff.api.v1.admin import require_super_admin_bff

router = APIRouter(tags=["Platform Admin"])

_TIMEOUT = 10
_METRIC = "storage_quota_used_bytes"

# Caps the range we'll ask Prometheus for in one call.  At step=5m a 7-day
# range is 2016 points per series — comfortably under Prometheus's default
# `query.max-samples` (~50M).  Beyond a week the chart UX falls apart anyway.
_MAX_RANGE_SECONDS = 7 * 24 * 3600
_DEFAULT_RANGE = "24h"
_DEFAULT_STEP = "5m"

_RANGE_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_range(s: str) -> int:
    """Parse a Prometheus-style duration (e.g. ``24h``, ``7d``) → seconds."""
    m = _RANGE_RE.match(s)
    if not m:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid range '{s}' (expected like '24h' / '7d' / '30m')",
        )
    n = int(m.group(1))
    unit = m.group(2)
    seconds = n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds <= 0 or seconds > _MAX_RANGE_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"range '{s}' is outside (0, 7d]",
        )
    return seconds


# Restrict label values to a conservative character set so we never inject a
# user-controlled string into the PromQL we send upstream.  PromQL label
# values are quoted, but the BFF still owns this validation as defence in
# depth — anything failing the pattern returns 400 immediately.
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _validate_label(name: str, value: str) -> str:
    if not _LABEL_VALUE_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{name} must match [A-Za-z0-9_.-]+",
        )
    return value


def _build_query(purpose: str | None, tenant_id: str | None) -> str:
    selectors: list[str] = []
    if purpose:
        selectors.append(
            f'bucket_purpose="{_validate_label("purpose", purpose)}"',
        )
    if tenant_id:
        selectors.append(
            f'tenant_id="{_validate_label("tenant_id", tenant_id)}"',
        )
    label_block = "{" + ",".join(selectors) + "}" if selectors else ""
    return f"{_METRIC}{label_block}"


@router.get("/admin/metrics/storage-quotas/series")
async def storage_quota_series(
    request: Request,
    _auth: tuple = Depends(require_super_admin_bff),
) -> dict:
    settings = request.app.state.settings
    range_str = request.query_params.get("range") or _DEFAULT_RANGE
    step = request.query_params.get("step") or _DEFAULT_STEP
    purpose = request.query_params.get("purpose")
    tenant_id = request.query_params.get("tenant_id")

    # Validate inputs before talking to Prometheus.
    range_seconds = _parse_range(range_str)
    if not _RANGE_RE.match(step):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid step '{step}'",
        )
    query = _build_query(purpose, tenant_id)

    end = int(time.time())
    start = end - range_seconds

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{settings.prometheus_url}/api/v1/query_range",
                params={
                    "query": query,
                    "start": str(start),
                    "end": str(end),
                    "step": step,
                },
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"prometheus upstream unreachable: {exc}",
        ) from exc

    if resp.status_code >= 400:
        # Surface Prometheus's own problem body verbatim so the operator can
        # see the upstream error message (e.g. parse error in PromQL).
        try:
            body = resp.json()
        except Exception:
            body = {"status": "error", "errorType": "upstream", "error": resp.text}
        raise HTTPException(status_code=resp.status_code, detail=body)

    return resp.json()
