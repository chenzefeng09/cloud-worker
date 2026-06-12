"""Foundation-DLP client for the attachment data-flow path.

Two helpers, both modelled on :mod:`agent_session_orchestrator.api.authz_guard`
(bare ``httpx.AsyncClient``, ``X-Tenant-Id`` forwarded as a header, tight
timeout):

``check_download(...)``
    Calls ``POST {dlp_url}/core/check:download`` before a file's bytes are
    served.  Policy is **fail-open with observability**:

    * DLP explicitly returns ``allowed=false``  → raise ``DlpDownloadDeniedError``
      (the caller turns this into HTTP 403 with the DLP reason).
    * DLP network error / timeout / non-200 / unparseable → **fail-open**:
      log a WARN and return (download proceeds).  This avoids a DLP outage
      bricking every download platform-wide.

      NOTE: this is a deliberate fail-OPEN.  To harden to fail-closed later,
      change the ``except`` / non-200 branches below to raise
      ``DlpDownloadDeniedError`` instead of returning.

``scan_and_tag(...)``
    Calls ``POST {dlp_url}/core/scan`` on text content and, when the returned
    ``action`` is not ``ALLOW``, auto-tags the resource via
    ``POST {dlp_url}/core/classification:tag`` with the scan's
    ``max_sensitivity``.  Fully **fail-open**: a hit never blocks the upload
    (per platform policy it is tagged, not rejected), and any DLP error is
    logged and swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Tight timeout — same posture as the authz guard (2.0s). DLP sits on the
# hot upload/download path; we never want it to add meaningful latency.
_DLP_TIMEOUT = 2.0  # seconds

# Fallback clearance for ``check_download`` when the caller does not pass an
# explicit ``user_clearance``.  The orchestrator now resolves a real per-user
# clearance from the ``X-User-Clearance`` header (see ``current_clearance`` in
# api/dependencies.py) and passes it through, so this is only a conservative
# safety net — kept in sync with that dependency's own "L2" fallback.
_DEFAULT_CLEARANCE = "L2"


class DlpDownloadDeniedError(Exception):
    """Raised when DLP explicitly denies a download (``allowed=false``).

    Carries the DLP-provided ``reason`` so the API layer can surface it in the
    403 detail.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


async def check_download(
    *,
    dlp_url: str,
    tenant_id: str,
    user_id: str,
    resource_id: str,
    resource_type: str = "attachment",
    user_clearance: str = _DEFAULT_CLEARANCE,
    destination: str = "laptop",
) -> None:
    """Enforce DLP ``check:download`` for a single resource.

    Raises ``DlpDownloadDeniedError`` only when DLP returns ``allowed=false``.
    Every other outcome (network error, timeout, non-200, unparseable body)
    is fail-OPEN: a WARN is logged and the function returns normally.
    """
    try:
        async with httpx.AsyncClient(timeout=_DLP_TIMEOUT) as client:
            resp = await client.post(
                f"{dlp_url}/core/check:download",
                json={
                    "user_id": user_id,
                    "user_clearance": user_clearance,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "destination": destination,
                },
                headers={"X-Tenant-Id": tenant_id},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        # fail-OPEN: do not block downloads on DLP unavailability.
        logger.warning(
            "DLP check:download unreachable (fail-open, allowing download)",
            extra={"resource_id": resource_id, "err": str(exc)},
        )
        return

    if resp.status_code != 200:
        # fail-OPEN on unexpected status.
        logger.warning(
            "DLP check:download returned HTTP %s (fail-open, allowing download)",
            resp.status_code,
            extra={"resource_id": resource_id},
        )
        return

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning(
            "DLP check:download response unparseable (fail-open)",
            extra={"resource_id": resource_id},
        )
        return

    if body.get("allowed") is False:
        reason = str(body.get("reason") or "blocked by data-loss-prevention policy")
        logger.info(
            "DLP check:download DENIED",
            extra={
                "resource_id": resource_id,
                "sensitivity": body.get("sensitivity"),
                "reason": reason,
            },
        )
        raise DlpDownloadDeniedError(reason)

    # allowed is True (or absent) → proceed.
    return


async def scan_and_tag(
    *,
    dlp_url: str,
    tenant_id: str,
    content: str,
    resource_id: str,
    resource_type: str = "attachment",
) -> None:
    """Scan ``content`` and auto-tag the resource if the scan finds a hit.

    Fully fail-open: any DLP error is logged and swallowed, and a hit never
    blocks the caller — the resource is classification-tagged with the scan's
    ``max_sensitivity`` instead.  Call this only with text content (callers
    must skip binary blobs).
    """
    try:
        async with httpx.AsyncClient(timeout=_DLP_TIMEOUT) as client:
            resp = await client.post(
                f"{dlp_url}/core/scan",
                json={
                    "content": content,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
                headers={"X-Tenant-Id": tenant_id},
            )
            if resp.status_code != 200:
                logger.warning(
                    "DLP scan returned HTTP %s (fail-open, skipping)",
                    resp.status_code,
                    extra={"resource_id": resource_id},
                )
                return

            try:
                body: dict[str, Any] = resp.json()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "DLP scan response unparseable (fail-open)",
                    extra={"resource_id": resource_id},
                )
                return

            action = body.get("action")
            max_sensitivity = body.get("max_sensitivity")
            hits = body.get("hits") or []

            if action == "ALLOW":
                # Nothing sensitive — no tag needed.
                return

            logger.warning(
                "DLP scan flagged content (action=%s, hits=%d, sensitivity=%s)",
                action,
                len(hits),
                max_sensitivity,
                extra={"resource_id": resource_id},
            )

            if not max_sensitivity:
                # No sensitivity to tag with — log only.
                return

            # Auto-tag the resource with the detected sensitivity.
            tag_resp = await client.post(
                f"{dlp_url}/core/classification:tag",
                json={
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "sensitivity": max_sensitivity,
                    "reason": "auto by upload scan",
                },
                headers={"X-Tenant-Id": tenant_id},
            )
            if tag_resp.status_code != 200:
                logger.warning(
                    "DLP classification:tag returned HTTP %s (fail-open)",
                    tag_resp.status_code,
                    extra={"resource_id": resource_id},
                )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        # fail-OPEN: DLP unavailability never blocks an upload.
        logger.warning(
            "DLP scan/tag unreachable (fail-open, skipping)",
            extra={"resource_id": resource_id, "err": str(exc)},
        )
