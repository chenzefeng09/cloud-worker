"""BFF notifications/preferences endpoints.

Prefix: /notifications

All endpoints require normal authentication (session cookie).  The caller's
user_id and tenant_id are taken from the signed session cookie.

Design: preferences are stored per (user_id, event_key) row.  A GET always
returns all known events, filling in defaults (email=True, push=True) for any
event not yet stored.  A PUT body is a list of {event_key, email_enabled,
push_enabled}; existing rows are upserted, unknown event keys outside the
canonical list are silently ignored.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from agent_bff.api.dependencies import require_auth
from agent_bff.domain import db as db_module
from agent_bff.domain.models import NotificationPreference, new_id, utcnow

router = APIRouter(tags=["Notifications"])
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical event catalogue (frontend labels are in the SPA; we store keys)
# ─────────────────────────────────────────────────────────────────────────────

CANONICAL_EVENTS: list[dict[str, Any]] = [
    {
        "event_key": "task_completed",
        "label": "Task completed",
        "sub": "Worker finished a task",
        "default_email": True,
        "default_push": True,
    },
    {
        "event_key": "needs_review",
        "label": "Needs review",
        "sub": "Worker paused for your input",
        "default_email": True,
        "default_push": True,
    },
    {
        "event_key": "budget_80pct",
        "label": "Budget at 80%",
        "sub": "Daily spend nearing limit",
        "default_email": True,
        "default_push": False,
    },
    {
        "event_key": "budget_hit",
        "label": "Budget limit reached",
        "sub": "Worker stopped — limit reached",
        "default_email": True,
        "default_push": True,
    },
    {
        "event_key": "worker_error",
        "label": "Worker error",
        "sub": "Unrecoverable failure",
        "default_email": True,
        "default_push": True,
    },
    {
        "event_key": "weekly_digest",
        "label": "Weekly digest",
        "sub": "Summary of activity & spend",
        "default_email": True,
        "default_push": False,
    },
]

_CANONICAL_KEYS = {e["event_key"] for e in CANONICAL_EVENTS}
_DEFAULT_EMAIL = {e["event_key"]: e["default_email"] for e in CANONICAL_EVENTS}
_DEFAULT_PUSH = {e["event_key"]: e["default_push"] for e in CANONICAL_EVENTS}


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class NotifPreferenceItem(BaseModel):
    event_key: str
    label: str
    sub: str
    email_enabled: bool
    push_enabled: bool


class NotifPreferencesResponse(BaseModel):
    items: list[NotifPreferenceItem]


class NotifPreferenceUpdate(BaseModel):
    event_key: str
    email_enabled: bool
    push_enabled: bool


class NotifPreferencesPutBody(BaseModel):
    items: list[NotifPreferenceUpdate]


# ─────────────────────────────────────────────────────────────────────────────
# GET /notifications/preferences
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/notifications/preferences", response_model=NotifPreferencesResponse)
async def get_preferences(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> NotifPreferencesResponse:
    """Return all notification preferences for the authenticated user.

    Events not yet stored in DB are returned with their per-event defaults
    (never 404).
    """
    tenant_id, user_id, _token = _auth

    async for session in db_module.get_session(tenant_id=tenant_id, user_id=user_id):
        rows = (
            await session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id
                )
            )
        ).scalars().all()

    stored: dict[str, NotificationPreference] = {r.event_key: r for r in rows}

    items: list[NotifPreferenceItem] = []
    for ev in CANONICAL_EVENTS:
        key = ev["event_key"]
        if key in stored:
            row = stored[key]
            email = row.email_enabled
            push = row.push_enabled
        else:
            email = _DEFAULT_EMAIL[key]
            push = _DEFAULT_PUSH[key]
        items.append(
            NotifPreferenceItem(
                event_key=key,
                label=ev["label"],
                sub=ev["sub"],
                email_enabled=email,
                push_enabled=push,
            )
        )

    return NotifPreferencesResponse(items=items)


# ─────────────────────────────────────────────────────────────────────────────
# PUT /notifications/preferences
# ─────────────────────────────────────────────────────────────────────────────

@router.put("/notifications/preferences", response_model=NotifPreferencesResponse)
async def update_preferences(
    body: NotifPreferencesPutBody,
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> NotifPreferencesResponse:
    """Upsert notification preferences for the authenticated user.

    Only items whose event_key is in the canonical list are persisted; unknown
    keys are silently ignored.  Returns the full updated preference list.
    """
    tenant_id, user_id, _token = _auth
    now = utcnow()

    # Filter to canonical keys only
    valid_updates = [u for u in body.items if u.event_key in _CANONICAL_KEYS]

    if valid_updates:
        async for session in db_module.get_session(tenant_id=tenant_id, user_id=user_id):
            bind = session.get_bind()
            is_postgres = bind is not None and bind.dialect.name == "postgresql"

            if is_postgres:
                # Use Postgres upsert (INSERT … ON CONFLICT DO UPDATE)
                values_list = [
                    {
                        "id": new_id("np"),
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "event_key": u.event_key,
                        "email_enabled": u.email_enabled,
                        "push_enabled": u.push_enabled,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for u in valid_updates
                ]
                stmt = pg_insert(NotificationPreference).values(values_list)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_notif_user_event",
                    set_={
                        "email_enabled": stmt.excluded.email_enabled,
                        "push_enabled": stmt.excluded.push_enabled,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
                await session.execute(stmt)
            else:
                # SQLite: load-then-update for test compatibility
                for u in valid_updates:
                    result = await session.execute(
                        select(NotificationPreference).where(
                            NotificationPreference.user_id == user_id,
                            NotificationPreference.event_key == u.event_key,
                        )
                    )
                    row = result.scalar_one_or_none()
                    if row is None:
                        row = NotificationPreference(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            event_key=u.event_key,
                            email_enabled=u.email_enabled,
                            push_enabled=u.push_enabled,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(row)
                    else:
                        row.email_enabled = u.email_enabled
                        row.push_enabled = u.push_enabled
                        row.updated_at = now

    # Re-read and return full list (same logic as GET)
    async for session in db_module.get_session(tenant_id=tenant_id, user_id=user_id):
        rows = (
            await session.execute(
                select(NotificationPreference).where(
                    NotificationPreference.user_id == user_id
                )
            )
        ).scalars().all()

    stored = {r.event_key: r for r in rows}
    items = []
    for ev in CANONICAL_EVENTS:
        key = ev["event_key"]
        if key in stored:
            row = stored[key]
            email = row.email_enabled
            push = row.push_enabled
        else:
            email = _DEFAULT_EMAIL[key]
            push = _DEFAULT_PUSH[key]
        items.append(
            NotifPreferenceItem(
                event_key=key,
                label=ev["label"],
                sub=ev["sub"],
                email_enabled=email,
                push_enabled=push,
            )
        )

    return NotifPreferencesResponse(items=items)
