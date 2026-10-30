"""GDPR self-service privacy API — export, erasure, settings, memories, notice.

Prefix: /api/bff/v1 — every route requires the session cookie (``require_auth``).

    GET    /me/privacy/export                  → JSON download (Art. 15 / 20)
    POST   /me/privacy/erase                   → erase my data (Art. 17)
    GET    /me/privacy/settings                → orchestrator GET /privacy/settings
    PUT    /me/privacy/settings                → orchestrator PUT /privacy/settings
    GET    /me/memories                        → knowledge-ingest GET /memories
    DELETE /me/memories/{memory_id}            → knowledge-ingest DELETE /memories/{id}
    DELETE /me/memories                        → knowledge-ingest DELETE /memories?all=true
    GET    /privacy/notice                     → server-side privacy notice (Art. 13)
    POST   /me/privacy/acknowledgements        → record that the notice was read
    GET    /me/privacy/acknowledgements/latest

Where a user's data lives
─────────────────────────
* session-orchestrator — sessions, messages, runs, files, canvases, workspaces
  and the memory setting.  It owns their export and erasure
  (``/privacy/export``, ``/privacy/erase``).
  Sessions that scheduled runs started on the user's behalf count as theirs.
* knowledge-ingest — the "[memory] …" session-memory documents (``/memories``).
* cron-runner — the schedules and webhook triggers the user created (internal
  ``/internal/privacy/users/{id}/cron-jobs``, service token).
* llm-gateway — the user's model preferences (``/user-preferences`` for the
  caller, ``/users/{id}/preferences`` for a tenant admin).
* app-registry — the Apps the user authored and the review-trail entries
  naming them (``/privacy/apps``).  Erasure deletes the Apps only they could
  see and keeps the ones shared with the tenant without naming them.
* this BFF — ``notification_preferences``, ``api_keys`` (metadata only; the key
  hash never leaves the database), ``privacy_notice_acknowledgements`` and the
  browser sign-ins in ``web_sessions`` (exported as when each started, was last
  refreshed and ends; the stored tokens never leave the database).  Erasure
  ends every sign-in of the subject except the one the request itself came
  from: the account stays open, and the user is still looking at the result.
* foundation identity — the sign-in account (exported as ``profile``).  Closing
  it is an administrator action, so erasure leaves it in place and says so in
  its response.

Every part of an export is required: an export that silently missed one would
misstate what is held, so any upstream failure fails the export (502).

Erasure first runs a preflight that touches nothing: each step's service is
asked, with the same identity the erasure will use, for what it would delete
(dry runs on the orchestrator and app-registry, listings elsewhere).  A service
that is unreachable or refuses refuses the whole erasure — 503, nothing erased
— so a deterministic failure (a missing permission, a misconfigured URL) can no
longer leave an erasure half done.  Then the steps run in a fixed order and
stop at the first failing one: the user's schedules first (so none starts
another run on their behalf), the orchestrator (which also cancels live runs,
so no new memory is written afterwards), knowledge-ingest memories, the LLM
preferences, the Apps, then the BFF rows.  A step this deployment cannot
perform is reported as skipped with the reason, never as done.  Every step is
idempotent, so after a partial failure (502 naming the steps that finished)
the request is simply repeated.

Between the preflight and the first step, the request is written to the
erasure ledger (``domain/erasure_ledger.py``), which a backup restore does not
rewind, so ``agent_bff.erasure_replay`` can erase the restored rows again.  A
configured ledger that can't be written refuses the erasure (503, nothing
erased): an erasure a restore would silently undo is not performed.

The export and erasure fan-outs are plain functions so the tenant-admin DSAR
routes (``admin_privacy.py``) and the post-restore replay run the same code
with admin headers.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import delete, select

from agent_bff.api._http import foundation_headers
from agent_bff.api.dependencies import require_auth
from agent_bff.api.middleware import get_web_session_id
from agent_bff.api.v1.admin import _raise_for_upstream
from agent_bff.config import Settings
from agent_bff.domain import db as db_module
from agent_bff.domain.erasure_ledger import ErasureLedger, LedgerError
from agent_bff.domain.models import ApiKey, NotificationPreference, PrivacyNoticeAcknowledgement
from agent_bff.domain.web_sessions import WebSession

router = APIRouter(tags=["Privacy"])
logger = logging.getLogger(__name__)

ERASE_CONFIRMATION = "DELETE MY DATA"
SELF_ACCOUNT_NOTE = "Your sign-in account itself is closed by your administrator."

USER_ID_PATTERN = r"^usr_[0-9A-Z]{26}$"
_MEMORY_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

_ORCHESTRATOR = "session-orchestrator"
_KNOWLEDGE = "knowledge-ingest"
_CRON = "cron-runner"
_LLM = "llm-gateway"
_APPS = "app-registry"
_IDENTITY = "identity"

_TIMEOUT = 15
# The orchestrator walks every session the user owns for an export / erasure.
_FANOUT_TIMEOUT = 120
# Best-effort lookups (the notice's retention and memory setting) must not hold
# up a response.
_SOFT_TIMEOUT = 5
_AUDIT_TIMEOUT = 5

_ERASE_STEPS = ("schedules", "platform", "memories", "llm_preferences", "apps", "bff")
# A gateway purpose becomes a URL path segment, so it must be a plain token.
_LLM_PURPOSE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Data-subject rights listed in the notice (GDPR Art. 13(2)(b)-(d)).
_RIGHTS: tuple[str, ...] = (
    "Access your personal data and get a copy of it (GDPR Art. 15)",
    "Have inaccurate personal data corrected (Art. 16)",
    "Have your personal data erased (Art. 17)",
    "Restrict how your personal data is processed (Art. 18)",
    "Receive your data in a portable, machine-readable format (Art. 20)",
    "Object to processing based on legitimate interests, such as session memory (Art. 21)",
    "Not be subject to decisions based solely on automated processing that "
    "significantly affect you (Art. 22)",
    "Withdraw consent at any time, without affecting earlier processing (Art. 7(3))",
    "Lodge a complaint with a data protection supervisory authority (Art. 77)",
)


# ─────────────────────────────────────────────────────────────────────────────
# Data subject + upstream helpers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DataSubject:
    """Whose data a privacy operation touches, and who is asking.

    Self-service: ``user_id`` is the caller.  Admin DSAR: ``user_id`` is the
    data subject and ``actor_user_id`` the tenant admin; upstream calls then
    carry ``X-Actor: ADMIN`` and name the subject explicitly.
    """

    tenant_id: str
    user_id: str
    actor_user_id: str
    access_token: str
    admin: bool = False
    # ``web_sessions.id`` of the sign-in the request came from: erasure never
    # ends it (``""`` = none, e.g. the post-restore replay).
    keep_web_session: str = ""

    @classmethod
    def from_auth(
        cls, auth: tuple[str, str, str], *, keep_web_session: str = "",
    ) -> DataSubject:
        tenant_id, user_id, access_token = auth
        return cls(
            tenant_id, user_id, user_id, access_token, keep_web_session=keep_web_session,
        )

    @classmethod
    def for_admin(
        cls, auth: tuple[str, str, str], subject_user_id: str, *, keep_web_session: str = "",
    ) -> DataSubject:
        tenant_id, actor_user_id, access_token = auth
        return cls(
            tenant_id, subject_user_id, actor_user_id, access_token,
            admin=True, keep_web_session=keep_web_session,
        )

    def plain_headers(self) -> dict[str, str]:
        return foundation_headers(self.tenant_id, self.actor_user_id, self.access_token)

    def headers(self) -> dict[str, str]:
        """Headers for the orchestrator / knowledge-ingest privacy surfaces."""
        headers = self.plain_headers()
        if self.admin:
            headers["X-Actor"] = "ADMIN"
        return headers


class UpstreamError(Exception):
    """A required upstream call failed (unreachable, non-2xx or unusable body)."""

    def __init__(self, service: str, reason: str) -> None:
        super().__init__(f"{service} {reason}")
        self.service = service
        self.reason = reason


async def _send(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    kwargs: dict[str, Any] = {"headers": headers}
    if params is not None:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    async with httpx.AsyncClient(timeout=timeout) as client:
        response: httpx.Response = await getattr(client, method.lower())(url, **kwargs)
    return response


async def _call(
    method: str,
    url: str,
    *,
    service: str,
    headers: dict[str, str],
    timeout: float = _TIMEOUT,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> Any:
    """One upstream call → its JSON body; raises ``UpstreamError`` on any failure."""
    try:
        resp = await _send(
            method, url, headers=headers, timeout=timeout, params=params, json_body=json_body,
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(service, f"unreachable ({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        raise UpstreamError(service, f"returned HTTP {resp.status_code}")
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError(service, "returned a non-JSON body") from exc


async def _call_soft(method: str, url: str, *, service: str, headers: dict[str, str]) -> Any:
    """Best-effort variant of ``_call``: ``None`` instead of an error."""
    try:
        return await _call(method, url, service=service, headers=headers, timeout=_SOFT_TIMEOUT)
    except UpstreamError as exc:
        logger.warning("privacy: optional %s lookup failed: %s", exc.service, exc.reason)
        return None


async def _proxy(
    method: str,
    url: str,
    *,
    service: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Transparent proxy: upstream 4xx/5xx is relayed, unreachable → 502."""
    try:
        resp = await _send(
            method, url, headers=headers, timeout=_TIMEOUT, params=params, json_body=json_body,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "user_message": "This service is temporarily unavailable. Please try again.",
                "service": service,
            },
        ) from exc
    _raise_for_upstream(resp)
    return resp


def _json_body(resp: httpx.Response) -> Any:
    return resp.json() if resp.content else {}


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite hands back naive UTC timestamps
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _new_event_id() -> str:
    """``evt_<26 upper-alnum>`` — the id format foundation-audit requires."""
    return f"evt_{uuid.uuid4().hex[:26].upper()}"


async def emit_privacy_audit(
    settings: Settings,
    subject: DataSubject,
    *,
    event_type: str,
    outcome: str,
    payload: dict[str, Any],
) -> None:
    """Best-effort audit event for a privacy action (never blocks the action).

    Same contract as ``sessions._emit_session_view_audit``: the canonical
    ``AuditEvent`` posted to foundation-audit ``/events``; failures are logged.
    The payload carries step names and counts only — never exported content.
    """
    try:
        body = {
            "event_id": _new_event_id(),
            "occurred_at": datetime.now(UTC).isoformat(),
            "tenant_id": subject.tenant_id,
            "service": "agent-bff",
            "event_type": event_type,
            "severity": "NOTICE",
            "outcome": outcome,
            "actor_kind": "ADMIN" if subject.admin else "USER",
            "actor_user_id": subject.actor_user_id,
            "resource_type": "user",
            "resource_id": subject.user_id,
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_AUDIT_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_audit_url}/events",
                json=body,
                headers=subject.plain_headers(),
            )
        if resp.status_code >= 400:
            logger.warning("privacy audit rejected (%s): %s", resp.status_code, event_type)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort, never blocks
        logger.warning("privacy audit emit failed (%s): %s", event_type, exc)


# ─────────────────────────────────────────────────────────────────────────────
# BFF-local rows
# ─────────────────────────────────────────────────────────────────────────────

_BFF_TABLES = (NotificationPreference, ApiKey, PrivacyNoticeAcknowledgement)


def _db_actor(subject: DataSubject) -> str:
    return "ADMIN" if subject.admin else "USER"


async def _bff_rows(subject: DataSubject) -> dict[str, list[dict[str, Any]]]:
    """The subject's BFF-local rows, export-shaped (API-key hashes excluded)."""
    async for session in db_module.get_session(
        tenant_id=subject.tenant_id, user_id=subject.user_id, actor=_db_actor(subject),
    ):
        prefs = (
            await session.execute(
                select(NotificationPreference)
                .where(
                    NotificationPreference.tenant_id == subject.tenant_id,
                    NotificationPreference.user_id == subject.user_id,
                )
                .order_by(NotificationPreference.event_key)
            )
        ).scalars().all()
        keys = (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.tenant_id == subject.tenant_id, ApiKey.user_id == subject.user_id)
                .order_by(ApiKey.created_at)
            )
        ).scalars().all()
        acks = (
            await session.execute(
                select(PrivacyNoticeAcknowledgement)
                .where(
                    PrivacyNoticeAcknowledgement.tenant_id == subject.tenant_id,
                    PrivacyNoticeAcknowledgement.user_id == subject.user_id,
                )
                .order_by(PrivacyNoticeAcknowledgement.acknowledged_at)
            )
        ).scalars().all()
        sign_ins = (
            await session.execute(
                select(WebSession)
                .where(
                    WebSession.tenant_id == subject.tenant_id,
                    WebSession.user_id == subject.user_id,
                )
                .order_by(WebSession.created_at)
            )
        ).scalars().all()
    return {
        "notification_preferences": [
            {
                "event_key": p.event_key,
                "email_enabled": p.email_enabled,
                "push_enabled": p.push_enabled,
                "created_at": _iso(p.created_at),
                "updated_at": _iso(p.updated_at),
            }
            for p in prefs
        ],
        "api_keys": [
            {
                "id": k.id,
                "name": k.name,
                "key_prefix": k.key_prefix,
                "created_at": _iso(k.created_at),
                "last_used_at": _iso(k.last_used_at),
                "revoked_at": _iso(k.revoked_at),
            }
            for k in keys
        ],
        "privacy_acknowledgements": [
            {"notice_version": a.notice_version, "acknowledged_at": _iso(a.acknowledged_at)}
            for a in acks
        ],
        # Times only: the session id and the stored tokens are credentials.
        "sign_in_sessions": [
            {
                "created_at": _iso(s.created_at),
                "refreshed_at": _iso(s.refreshed_at),
                "expires_at": _iso(s.expires_at),
            }
            for s in sign_ins
        ],
    }


async def _erase_bff_rows(subject: DataSubject) -> dict[str, int]:
    counts: dict[str, int] = {}
    async for session in db_module.get_session(
        tenant_id=subject.tenant_id, user_id=subject.user_id, actor=_db_actor(subject),
    ):
        for model in _BFF_TABLES:
            result = await session.execute(
                delete(model).where(
                    model.tenant_id == subject.tenant_id, model.user_id == subject.user_id,
                )
            )
            counts[model.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
        # Deleting a sign-in is what signs that browser out (its tokens live
        # only in this row).  The one the request came from stays.
        sign_ins = delete(WebSession).where(
            WebSession.tenant_id == subject.tenant_id, WebSession.user_id == subject.user_id,
        )
        if subject.keep_web_session:
            sign_ins = sign_ins.where(WebSession.id != subject.keep_web_session)
        result = await session.execute(sign_ins)
        counts[WebSession.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Schedules (cron-runner) and LLM model preferences (llm-gateway)
# ─────────────────────────────────────────────────────────────────────────────

def _schedules_url(settings: Settings, subject: DataSubject) -> str | None:
    """cron-runner's privacy endpoint for the subject, or ``None`` when not deployed.

    The endpoint is service-to-service (shared service token, the same one
    cron-runner presents to this BFF), so it needs the cron-runner URL and
    ``service_token``; without either this deployment runs no schedules.
    """
    base = settings.cron_runner_url.rstrip("/")
    if not base or not settings.service_token:
        return None
    return f"{base}/internal/privacy/users/{subject.user_id}/cron-jobs"


def _cron_headers(settings: Settings, subject: DataSubject) -> dict[str, str]:
    return {"X-Service-Token": settings.service_token, "X-Tenant-Id": subject.tenant_id}


async def _export_schedules(
    settings: Settings, subject: DataSubject,
) -> list[dict[str, Any]] | None:
    url = _schedules_url(settings, subject)
    if url is None:
        return None
    body = await _call("GET", url, service=_CRON, headers=_cron_headers(settings, subject))
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise UpstreamError(_CRON, "returned an unexpected schedule listing")
    return items


def _llm_preferences_url(settings: Settings, subject: DataSubject) -> str:
    """The gateway answers ``/user-preferences`` for the caller only; a tenant
    admin reads and deletes a member's through ``/users/{id}/preferences``."""
    base = settings.foundation_llm_gateway_url.rstrip("/")
    if subject.admin:
        return f"{base}/users/{subject.user_id}/preferences"
    return f"{base}/user-preferences"


def _llm_headers(subject: DataSubject) -> dict[str, str]:
    # ``X-Actor: ADMIN`` is what the gateway's member routes require; the
    # self-service routes act on the caller, so no actor header there.
    return subject.headers() if subject.admin else subject.plain_headers()


async def _export_llm_preferences(
    settings: Settings, subject: DataSubject,
) -> list[dict[str, Any]]:
    body = await _call(
        "GET", _llm_preferences_url(settings, subject), service=_LLM,
        headers=_llm_headers(subject),
    )
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise UpstreamError(_LLM, "returned an unexpected preference listing")
    return items


def _llm_purposes(items: list[dict[str, Any]]) -> list[str]:
    purposes: list[str] = []
    for item in items:
        purpose = item.get("purpose") if isinstance(item, dict) else None
        if not isinstance(purpose, str) or not _LLM_PURPOSE_RE.fullmatch(purpose):
            raise UpstreamError(_LLM, "returned a preference without a usable purpose")
        if purpose not in purposes:
            purposes.append(purpose)
    return purposes


async def _erase_llm_preferences(settings: Settings, subject: DataSubject) -> int:
    """Delete every LLM model preference the gateway keeps for the subject.

    A tenant admin deletes a member's in one call (``DELETE
    /users/{id}/preferences`` → ``{"deleted": n}``).  The caller's own are
    listed and deleted one purpose at a time; a purpose that is already gone
    (404) counts as deleted, so a repeat is harmless.
    """
    if subject.admin:
        body = await _call(
            "DELETE", _llm_preferences_url(settings, subject), service=_LLM,
            headers=_llm_headers(subject),
        )
        return _deleted_count(body, _LLM)
    base = _llm_preferences_url(settings, subject)
    headers = _llm_headers(subject)
    purposes = _llm_purposes(await _export_llm_preferences(settings, subject))
    for purpose in purposes:
        try:
            resp = await _send("DELETE", f"{base}/{purpose}", headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise UpstreamError(_LLM, f"unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 400 and resp.status_code != 404:
            raise UpstreamError(_LLM, f"returned HTTP {resp.status_code}")
    return len(purposes)


# ─────────────────────────────────────────────────────────────────────────────
# Apps (app-registry)
# ─────────────────────────────────────────────────────────────────────────────

def _apps_url(settings: Settings, path: str) -> str:
    return f"{settings.app_registry_url.rstrip('/')}/privacy/apps{path}"


async def _export_apps(settings: Settings, subject: DataSubject) -> dict[str, Any]:
    body = await _call(
        "GET",
        _apps_url(settings, ""),
        service=_APPS,
        headers=subject.headers(),
        params={"user_id": subject.user_id} if subject.admin else None,
    )
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("apps"), list)
        or not isinstance(body.get("review_trail"), list)
    ):
        raise UpstreamError(_APPS, "returned an unexpected App listing")
    return {"authored": body["apps"], "review_trail": body["review_trail"]}


async def _erase_apps(
    settings: Settings, subject: DataSubject, *, dry_run: bool,
) -> dict[str, int]:
    erase_body: dict[str, Any] = {"dry_run": dry_run}
    if subject.admin:
        erase_body["user_id"] = subject.user_id
    body = await _call(
        "POST", _apps_url(settings, "/erase"), service=_APPS, headers=subject.headers(),
        json_body=erase_body,
    )
    deleted = body.get("deleted") if isinstance(body, dict) else None
    if not isinstance(deleted, dict) or not all(
        isinstance(v, int) for v in deleted.values()
    ):
        raise UpstreamError(_APPS, "returned an unexpected erasure result")
    return {str(k): int(v) for k, v in deleted.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────

def _memories_url(settings: Settings, subject: DataSubject) -> str:
    if subject.admin:
        return f"{settings.knowledge_ingest_url}/internal/privacy/users/{subject.user_id}/memories"
    return f"{settings.knowledge_ingest_url}/memories"


async def build_export(settings: Settings, subject: DataSubject) -> dict[str, Any]:
    """Everything the platform holds about ``subject``, as one JSON document.

    Every part is required — an export that silently missed one would misstate
    what is held — so any failure raises ``UpstreamError``; ``schedules`` is
    ``null`` only where cron-runner is not deployed.
    """
    headers = subject.headers()
    platform = await _call(
        "GET",
        f"{settings.session_orchestrator_url}/privacy/export",
        service=_ORCHESTRATOR,
        headers=headers,
        timeout=_FANOUT_TIMEOUT,
        params={"user_id": subject.user_id} if subject.admin else None,
    )
    memories = await _call(
        "GET", _memories_url(settings, subject), service=_KNOWLEDGE, headers=headers,
    )
    if not isinstance(memories, dict) or not isinstance(memories.get("items"), list):
        raise UpstreamError(_KNOWLEDGE, "returned an unexpected memory listing")
    schedules = await _export_schedules(settings, subject)

    if subject.admin:
        profile_url = f"{settings.foundation_identity_url}/users/{subject.user_id}"
    else:
        profile_url = f"{settings.foundation_identity_url}/sessions/me"
    profile = await _call("GET", profile_url, service=_IDENTITY, headers=subject.plain_headers())
    if not isinstance(profile, dict):
        raise UpstreamError(_IDENTITY, "returned an unexpected profile")

    llm_preferences = await _export_llm_preferences(settings, subject)
    apps = await _export_apps(settings, subject)
    rows = await _bff_rows(subject)

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "platform": platform,
        "memories": memories["items"],
        "schedules": schedules,
        "apps": apps,
        "preferences": {
            "notifications": rows["notification_preferences"],
            "llm": llm_preferences,
        },
        "api_keys": rows["api_keys"],
        "privacy_acknowledgements": rows["privacy_acknowledgements"],
        "sign_in_sessions": rows["sign_in_sessions"],
    }


def export_failed_detail(exc: UpstreamError) -> dict[str, str]:
    return {
        "user_message": (
            "The data export could not be completed because a platform service "
            "is unavailable. Please try again later."
        ),
        "code": "PRIVACY_EXPORT_UNAVAILABLE",
        "service": exc.service,
        "error": exc.reason,
    }


def export_download(payload: dict[str, Any], filename: str) -> JSONResponse:
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# Erasure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ErasureRun:
    """One erasure's bookkeeping: per-step results, finished and skipped steps."""

    results: dict[str, Any] = dc_field(default_factory=dict)
    succeeded: list[str] = dc_field(default_factory=list)
    skipped: dict[str, str] = dc_field(default_factory=dict)

    def skip(self, step: str, reason: str) -> None:
        """Record a step this deployment cannot perform — never as done."""
        self.skipped[step] = reason
        self.results[step] = {"skipped": reason}


class ErasureIncompleteError(Exception):
    """An erasure step failed; the steps before it finished (or were skipped)."""

    def __init__(self, *, run: ErasureRun, failed: str, error: str) -> None:
        super().__init__(f"erasure step {failed!r} failed: {error}")
        self.results = run.results
        self.succeeded = run.succeeded
        self.skipped = run.skipped
        self.failed = failed
        self.error = error

    @property
    def not_attempted(self) -> list[str]:
        done = {*self.succeeded, *self.skipped, self.failed}
        return [step for step in _ERASE_STEPS if step not in done]

    def detail(self) -> dict[str, Any]:
        return {
            "user_message": (
                "Your data could not be fully erased. The steps that finished are "
                "listed; it is safe to try again."
            ),
            "code": "PRIVACY_ERASURE_INCOMPLETE",
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "not_attempted": self.not_attempted,
            "error": self.error,
            "results": self.results,
        }


class ErasureRefusedError(ErasureIncompleteError):
    """The preflight failed: no step ran, so nothing was erased.

    An ``ErasureIncompleteError`` with no step done, so a caller that only
    knows the general case (the post-restore replay) still reports it as an
    incomplete erasure.
    """

    def __init__(self, *, failed: str, error: str) -> None:
        super().__init__(run=ErasureRun(), failed=failed, error=error)

    @property
    def not_attempted(self) -> list[str]:
        return list(_ERASE_STEPS)

    def detail(self) -> dict[str, Any]:
        return {
            "user_message": (
                "Nothing was erased: a platform service is unavailable or refused the "
                "request. Please try again later."
            ),
            "code": "PRIVACY_ERASURE_UNAVAILABLE",
            "failed": self.failed,
            "error": self.error,
        }


def _deleted_count(body: Any, service: str = _KNOWLEDGE) -> int:
    if isinstance(body, dict) and isinstance(body.get("deleted"), int):
        return int(body["deleted"])
    raise UpstreamError(service, "returned an unexpected erasure result")


async def preflight_erasure(settings: Settings, subject: DataSubject) -> dict[str, Any]:
    """Ask every step's service what it would erase, changing nothing.

    Each call uses the identity the erasure itself will use, so a missing
    permission, a misconfigured URL or an unreachable service shows up here,
    before anything is deleted.  Returns the per-step previews; raises
    ``ErasureRefusedError`` naming the first step that failed.
    """
    headers = subject.headers()
    preview: dict[str, Any] = {}
    step = _ERASE_STEPS[0]
    try:
        schedules = await _export_schedules(settings, subject)
        preview[step] = None if schedules is None else {"count": len(schedules)}

        step = "platform"
        dry_run_body: dict[str, Any] = {"dry_run": True}
        if subject.admin:
            dry_run_body["user_id"] = subject.user_id
        preview[step] = await _call(
            "POST",
            f"{settings.session_orchestrator_url}/privacy/erase",
            service=_ORCHESTRATOR,
            headers=headers,
            timeout=_FANOUT_TIMEOUT,
            json_body=dry_run_body,
        )

        step = "memories"
        memories = await _call(
            "GET", _memories_url(settings, subject), service=_KNOWLEDGE, headers=headers,
        )
        if not isinstance(memories, dict) or not isinstance(memories.get("items"), list):
            raise UpstreamError(_KNOWLEDGE, "returned an unexpected memory listing")
        preview[step] = {"count": len(memories["items"])}

        step = "llm_preferences"
        llm_preferences = await _export_llm_preferences(settings, subject)
        if not subject.admin:
            # The caller's own are deleted one purpose at a time: each must
            # make a usable URL segment before anything is deleted.
            _llm_purposes(llm_preferences)
        preview[step] = {"count": len(llm_preferences)}

        step = "apps"
        preview[step] = await _erase_apps(settings, subject, dry_run=True)

        step = "bff"
        rows = await _bff_rows(subject)
        preview[step] = {table: len(items) for table, items in rows.items()}
    except UpstreamError as exc:
        raise ErasureRefusedError(failed=step, error=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a local DB failure refuses it too
        logger.exception("privacy erasure preflight step %s failed", step)
        raise ErasureRefusedError(failed=step, error="internal error") from exc
    return preview


async def execute_erasure(settings: Settings, subject: DataSubject) -> ErasureRun:
    """Erase ``subject``'s data, one step at a time (see ``_ERASE_STEPS``).

    Schedules go first, so none can start another run on the user's behalf
    after the platform data is gone.  Stops at the first failing step and
    raises ``ErasureIncompleteError``; every step is idempotent, so the whole
    request can simply be repeated.  Run ``preflight_erasure`` first.
    """
    headers = subject.headers()
    run = ErasureRun()
    step = _ERASE_STEPS[0]
    try:
        schedules_url = _schedules_url(settings, subject)
        if schedules_url is None:
            run.skip(step, "not_configured")
        else:
            deleted = await _call(
                "DELETE", schedules_url, service=_CRON, headers=_cron_headers(settings, subject),
            )
            run.results[step] = {"deleted": _deleted_count(deleted, _CRON)}
            run.succeeded.append(step)

        step = "platform"
        erase_body: dict[str, Any] = {"dry_run": False}
        if subject.admin:
            erase_body["user_id"] = subject.user_id
        run.results["platform"] = await _call(
            "POST",
            f"{settings.session_orchestrator_url}/privacy/erase",
            service=_ORCHESTRATOR,
            headers=headers,
            timeout=_FANOUT_TIMEOUT,
            json_body=erase_body,
        )
        run.succeeded.append(step)

        step = "memories"
        memories = await _call(
            "DELETE",
            _memories_url(settings, subject),
            service=_KNOWLEDGE,
            headers=headers,
            timeout=_FANOUT_TIMEOUT,
            params=None if subject.admin else {"all": "true"},
        )
        run.results["memories_deleted"] = _deleted_count(memories)
        run.succeeded.append(step)

        step = "llm_preferences"
        run.results[step] = {"deleted": await _erase_llm_preferences(settings, subject)}
        run.succeeded.append(step)

        step = "apps"
        run.results[step] = await _erase_apps(settings, subject, dry_run=False)
        run.succeeded.append(step)

        step = "bff"
        run.results["bff"] = await _erase_bff_rows(subject)
        run.succeeded.append(step)
    except UpstreamError as exc:
        raise ErasureIncompleteError(run=run, failed=step, error=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — a local DB failure is a failed step too
        logger.exception("privacy erasure step %s failed", step)
        raise ErasureIncompleteError(run=run, failed=step, error="internal error") from exc
    return run


async def run_erasure(settings: Settings, subject: DataSubject) -> ErasureRun:
    """``preflight_erasure`` then ``execute_erasure`` (the post-restore replay).

    Raises ``ErasureRefusedError`` (nothing erased) or ``ErasureIncompleteError``.
    """
    await preflight_erasure(settings, subject)
    return await execute_erasure(settings, subject)


def erasure_counts(results: dict[str, Any]) -> dict[str, Any]:
    """Counts-only summary of an erasure for the audit payload."""
    counts: dict[str, Any] = {}
    platform = results.get("platform")
    if isinstance(platform, dict):
        counts["platform_deleted"] = platform.get("deleted")
        counts["runs_cancelled"] = platform.get("runs_cancelled")
    if "memories_deleted" in results:
        counts["memories_deleted"] = results["memories_deleted"]
    for step in ("schedules", "llm_preferences"):
        outcome = results.get(step)
        if isinstance(outcome, dict) and isinstance(outcome.get("deleted"), int):
            counts[f"{step}_deleted"] = outcome["deleted"]
    apps = results.get("apps")
    if isinstance(apps, dict) and all(isinstance(v, int) for v in apps.values()):
        counts["apps"] = apps
    if "bff" in results:
        counts["bff"] = results["bff"]
    return counts


async def _record_in_ledger(
    settings: Settings, subject: DataSubject, *, via: str, event_type: str,
) -> str:
    """Write the erasure request to the ledger before anything is erased.

    Returns ``"recorded"``, or ``"not_configured"`` when this deployment has no
    ledger (the erasure still runs, but a backup restore can't replay it).  A
    configured ledger that can't be written refuses the request: HTTP 503,
    audited, nothing erased.
    """
    ledger = ErasureLedger.from_settings(settings)
    if ledger is None:
        logger.warning(
            "privacy erasure ledger not configured: a backup restore cannot replay this erasure",
        )
        return "not_configured"
    try:
        await ledger.record(
            tenant_id=subject.tenant_id,
            user_id=subject.user_id,
            via=via,
            actor_user_id=subject.actor_user_id,
        )
    except LedgerError as exc:
        logger.error("privacy erasure refused, ledger unavailable: %s", exc)
        await emit_privacy_audit(
            settings,
            subject,
            event_type=event_type,
            outcome="FAILURE",
            payload={"via": via, "failed": "ledger"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "user_message": (
                    "The data could not be erased right now because the erasure could "
                    "not be recorded. Nothing was erased; please try again later."
                ),
                "code": "PRIVACY_LEDGER_UNAVAILABLE",
            },
        ) from exc
    return "recorded"


async def erase_with_audit(
    settings: Settings,
    subject: DataSubject,
    *,
    event_type: str,
    account_note: str,
) -> dict[str, Any]:
    """Preflight, ledger entry, the erasure, then an audit event either way.

    Preflight refused or ledger unavailable → HTTP 503 (nothing erased);
    incomplete → HTTP 502.
    """
    via = "admin_dsar" if subject.admin else "self_service"
    try:
        await preflight_erasure(settings, subject)
    except ErasureRefusedError as exc:
        logger.warning("privacy erasure refused by preflight: %s", exc)
        await emit_privacy_audit(
            settings,
            subject,
            event_type=event_type,
            outcome="FAILURE",
            payload={"via": via, "phase": "preflight", "failed": exc.failed},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.detail(),
        ) from exc
    ledger = await _record_in_ledger(settings, subject, via=via, event_type=event_type)
    try:
        run = await execute_erasure(settings, subject)
    except ErasureIncompleteError as exc:
        payload: dict[str, Any] = {
            "via": via,
            "ledger": ledger,
            "succeeded": exc.succeeded,
            "failed": exc.failed,
            **erasure_counts(exc.results),
        }
        if exc.skipped:
            payload["skipped"] = exc.skipped
        await emit_privacy_audit(
            settings,
            subject,
            event_type=event_type,
            outcome="PARTIAL" if exc.succeeded else "FAILURE",
            payload=payload,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail()) from exc
    payload = {
        "via": via, "ledger": ledger, "succeeded": run.succeeded, **erasure_counts(run.results),
    }
    if run.skipped:
        payload["skipped"] = run.skipped
    await emit_privacy_audit(
        settings, subject, event_type=event_type, outcome="SUCCESS", payload=payload,
    )
    return {**run.results, "account": account_note}


# ─────────────────────────────────────────────────────────────────────────────
# Privacy notice
# ─────────────────────────────────────────────────────────────────────────────

def _parse_retention(body: Any) -> dict[str, int | None]:
    retention: dict[str, int | None] = {"session_retention_days": None, "trash_grace_days": None}
    if isinstance(body, dict):
        for key in retention:
            value = body.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                retention[key] = value
    return retention


def _memory_enabled(body: Any) -> bool:
    if isinstance(body, dict) and isinstance(body.get("memory_enabled"), bool):
        return bool(body["memory_enabled"])
    # Unknown → the platform default (on): never under-state that memory may
    # be collected.
    return True


def notice_version(settings: Settings, content: dict[str, Any]) -> str:
    """A short hash of the notice content, optionally prefixed by a label."""
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    label = settings.privacy_notice_version.strip()
    return f"{label}.{digest}" if label else digest


async def build_notice(settings: Settings, subject: DataSubject) -> dict[str, Any]:
    """The privacy notice for this deployment plus the caller's memory setting.

    The version hashes everything except ``memory`` (the caller's own setting,
    not notice content).  Retention fails soft to nulls, which changes the
    version too — a user who acknowledges while retention is unknown has not
    seen it, and is asked again once it is back.
    """
    headers = subject.headers()
    retention_body, settings_body = await asyncio.gather(
        _call_soft(
            "GET", f"{settings.session_orchestrator_url}/privacy/retention",
            service=_ORCHESTRATOR, headers=headers,
        ),
        _call_soft(
            "GET", f"{settings.session_orchestrator_url}/privacy/settings",
            service=_ORCHESTRATOR, headers=headers,
        ),
    )
    content: dict[str, Any] = {
        "controller": {
            "name": settings.privacy_controller_name,
            "contact_email": settings.privacy_controller_contact_email,
        },
        "dpo_email": settings.privacy_dpo_email or None,
        "purposes": [p.model_dump() for p in settings.privacy_notice_purposes],
        "retention": _parse_retention(retention_body),
        "processors": [p.model_dump() for p in settings.privacy_notice_processors],
        "rights": list(_RIGHTS),
    }
    return {
        "version": notice_version(settings, content),
        "controller": content["controller"],
        "dpo_email": content["dpo_email"],
        "purposes": content["purposes"],
        "retention": content["retention"],
        "memory": {"enabled": _memory_enabled(settings_body)},
        "processors": content["processors"],
        "rights": content["rights"],
    }


async def _record_consent(settings: Settings, subject: DataSubject, version: str) -> None:
    """Mirror the acknowledgement into foundation data-protection (best-effort)."""
    try:
        async with httpx.AsyncClient(timeout=_SOFT_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.foundation_data_protection_url}/consent",
                json={
                    "user_id": subject.user_id,
                    "consent_type": "privacy_notice",
                    "purpose": f"Acknowledged privacy notice version {version}",
                    "granted": True,
                },
                headers=subject.plain_headers(),
            )
        if resp.status_code >= 400:
            logger.warning("privacy consent record rejected (%s)", resp.status_code)
    except Exception as exc:  # noqa: BLE001 — the BFF row is the record of truth
        logger.warning("privacy consent record failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Request bodies
# ─────────────────────────────────────────────────────────────────────────────

class EraseRequest(BaseModel):
    confirm: str


class PrivacySettingsUpdate(BaseModel):
    memory_enabled: StrictBool


class AcknowledgementRequest(BaseModel):
    notice_version: str = Field(min_length=1, max_length=100)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — export / erasure
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me/privacy/export")
async def export_my_data(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> JSONResponse:
    """Download everything the platform holds about the caller (JSON file)."""
    subject = DataSubject.from_auth(_auth)
    try:
        export = await build_export(request.app.state.settings, subject)
    except UpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=export_failed_detail(exc),
        ) from exc
    return export_download(export, filename=f"my-data-{today()}.json")


@router.post("/me/privacy/erase")
async def erase_my_data(
    request: Request,
    body: EraseRequest,
    _auth: tuple = Depends(require_auth()),
) -> dict[str, Any]:
    """Erase the caller's data across the platform (the account stays open)."""
    if body.confirm != ERASE_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "user_message": f'Type "{ERASE_CONFIRMATION}" to confirm.',
                "code": "PRIVACY_CONFIRMATION_REQUIRED",
                "field": "confirm",
            },
        )
    return await erase_with_audit(
        request.app.state.settings,
        DataSubject.from_auth(_auth, keep_web_session=get_web_session_id()),
        event_type="privacy.user.erased",
        account_note=SELF_ACCOUNT_NOTE,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Routes — privacy settings (orchestrator)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me/privacy/settings")
async def get_privacy_settings(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Any:
    settings = request.app.state.settings
    resp = await _proxy(
        "GET",
        f"{settings.session_orchestrator_url}/privacy/settings",
        service=_ORCHESTRATOR,
        headers=DataSubject.from_auth(_auth).headers(),
    )
    return _json_body(resp)


@router.put("/me/privacy/settings")
async def update_privacy_settings(
    request: Request,
    body: PrivacySettingsUpdate,
    _auth: tuple = Depends(require_auth()),
) -> Any:
    settings = request.app.state.settings
    resp = await _proxy(
        "PUT",
        f"{settings.session_orchestrator_url}/privacy/settings",
        service=_ORCHESTRATOR,
        headers=DataSubject.from_auth(_auth).headers(),
        json_body={"memory_enabled": body.memory_enabled},
    )
    return _json_body(resp)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — session memories (knowledge-ingest)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/me/memories")
async def list_my_memories(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Any:
    settings = request.app.state.settings
    resp = await _proxy(
        "GET",
        f"{settings.knowledge_ingest_url}/memories",
        service=_KNOWLEDGE,
        headers=DataSubject.from_auth(_auth).headers(),
    )
    return _json_body(resp)


@router.delete("/me/memories/{memory_id}", status_code=204)
async def delete_my_memory(
    request: Request,
    memory_id: str = Path(pattern=_MEMORY_ID_PATTERN),
    _auth: tuple = Depends(require_auth()),
) -> Response:
    settings = request.app.state.settings
    await _proxy(
        "DELETE",
        f"{settings.knowledge_ingest_url}/memories/{memory_id}",
        service=_KNOWLEDGE,
        headers=DataSubject.from_auth(_auth).headers(),
    )
    return Response(status_code=204)


@router.delete("/me/memories")
async def delete_all_my_memories(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> Any:
    settings = request.app.state.settings
    resp = await _proxy(
        "DELETE",
        f"{settings.knowledge_ingest_url}/memories",
        service=_KNOWLEDGE,
        headers=DataSubject.from_auth(_auth).headers(),
        params={"all": "true"},
    )
    return _json_body(resp)


# ─────────────────────────────────────────────────────────────────────────────
# Routes — privacy notice + acknowledgements
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/privacy/notice")
async def privacy_notice(
    request: Request,
    _auth: tuple = Depends(require_auth()),
) -> dict[str, Any]:
    return await build_notice(request.app.state.settings, DataSubject.from_auth(_auth))


@router.post("/me/privacy/acknowledgements", status_code=204)
async def acknowledge_notice(
    request: Request,
    body: AcknowledgementRequest,
    _auth: tuple = Depends(require_auth()),
) -> Response:
    """Record that the caller read notice ``notice_version`` (append-only)."""
    subject = DataSubject.from_auth(_auth)
    version = body.notice_version.strip()
    if not version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"user_message": "notice_version must not be blank.", "field": "notice_version"},
        )
    async for session in db_module.get_session(
        tenant_id=subject.tenant_id, user_id=subject.user_id,
    ):
        session.add(
            PrivacyNoticeAcknowledgement(
                tenant_id=subject.tenant_id, user_id=subject.user_id, notice_version=version,
            )
        )
    await _record_consent(request.app.state.settings, subject, version)
    return Response(status_code=204)


@router.get("/me/privacy/acknowledgements/latest")
async def latest_acknowledgement(
    _auth: tuple = Depends(require_auth()),
) -> dict[str, str | None]:
    subject = DataSubject.from_auth(_auth)
    row: PrivacyNoticeAcknowledgement | None = None
    async for session in db_module.get_session(
        tenant_id=subject.tenant_id, user_id=subject.user_id,
    ):
        row = (
            await session.execute(
                select(PrivacyNoticeAcknowledgement)
                .where(
                    PrivacyNoticeAcknowledgement.tenant_id == subject.tenant_id,
                    PrivacyNoticeAcknowledgement.user_id == subject.user_id,
                )
                .order_by(
                    PrivacyNoticeAcknowledgement.acknowledged_at.desc(),
                    PrivacyNoticeAcknowledgement.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if row is None:
        return {"notice_version": None, "acknowledged_at": None}
    return {"notice_version": row.notice_version, "acknowledged_at": _iso(row.acknowledged_at)}
