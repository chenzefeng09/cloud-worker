"""Best-effort admin audit events (GDPR Art. 5(2) accountability, Art. 32).

Every privileged read of a user's content (session replay, knowledge preview,
ops run listings) and every change to a compliance record posts one canonical
``AuditEvent`` to foundation-audit ``POST {foundation_audit_url}/events``.

This is a SIDE-CHANNEL: any failure (bad config, audit down, non-2xx,
timeout) is swallowed with a warning log and never propagates — the admin
action itself must succeed or fail on its own merits.  Payloads carry ids,
counts and field names only, never the content that was read or written.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from agent_bff.api._http import foundation_headers

logger = logging.getLogger(__name__)

# Short timeout so audit never holds up the admin response.
_AUDIT_TIMEOUT = 5


def new_event_id() -> str:
    """An ``evt_<26 upper-alnum>`` id (foundation-audit's ``event_id`` pattern)."""
    return f"evt_{uuid.uuid4().hex[:26].upper()}"


async def emit_admin_audit(
    settings: Any,
    *,
    tenant_id: str,
    user_id: str,
    access_token: str,
    event_type: str,
    resource_type: str,
    resource_id: str | None,
    payload: dict[str, Any],
) -> None:
    """Post one ADMIN-actor audit event; never raises.

    ``tenant_id`` is the tenant whose data was touched (the ``?tenant_id=``
    target for a cross-tenant super_admin), so the event lands in that
    tenant's audit trail where its controller can see it.
    """
    try:
        body = {
            "event_id": new_event_id(),
            "occurred_at": datetime.now(UTC).isoformat(),
            "tenant_id": tenant_id,
            "service": "agent-bff",
            "event_type": event_type,
            "outcome": "SUCCESS",
            "actor_kind": "ADMIN",
            "actor_user_id": user_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_AUDIT_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_audit_url}/events",
                json=body,
                headers=foundation_headers(tenant_id, user_id, access_token),
            )
        if resp.status_code >= 400:
            logger.warning("admin audit rejected (%s): %s", resp.status_code, event_type)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort, never blocks
        logger.warning("admin audit emit failed (%s): %s", event_type, exc)
