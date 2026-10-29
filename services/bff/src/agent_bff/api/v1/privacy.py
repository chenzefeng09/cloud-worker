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
* llm-gateway — the user's model preferences (``/user-preferences``).
* this BFF — ``notification_preferences``, ``api_keys`` (metadata only; the key
  hash never leaves the database) and ``privacy_notice_acknowledgements``.
* foundation identity — the sign-in account.  Closing it is an administrator
  action, so erasure leaves it in place and says so in its response.

Erasure runs in a fixed order and stops at the first failing step: the user's
schedules first (so none starts another run on their behalf), the orchestrator
(which also cancels live runs, so no new memory is written afterwards),
knowledge-ingest memories, the LLM preferences, then the BFF rows.  A step this
deployment cannot perform is reported as skipped with the reason, never as
done.  Every step is idempotent, so after a partial failure (502 naming the
steps that finished) the request is simply repeated.

The export and erasure fan-outs are plain functions so the tenant-admin DSAR
routes (``admin_privacy.py``) run the same code with admin headers.
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
from agent_bff.api.v1.admin import _raise_for_upstream
from agent_bff.config import Settings
from agent_bff.domain import db as db_module
from agent_bff.domain.models import ApiKey, NotificationPreference, PrivacyNoticeAcknowledgement

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

_TIMEOUT = 15
# The orchestrator walks every session the user owns for an export / erasure.
_FANOUT_TIMEOUT = 120
# Best-effort lookups (profile, notice retention) must not hold up a response.
_SOFT_TIMEOUT = 5
_AUDIT_TIMEOUT = 5

_ERASE_STEPS = ("schedules", "platform", "memories", "llm_preferences", "bff")
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

    @classmethod
    def from_auth(cls, auth: tuple[str, str, str]) -> DataSubject:
        tenant_id, user_id, access_token = auth
        return cls(tenant_id, user_id, user_id, access_token)

    @classmethod
    def for_admin(cls, auth: tuple[str, str, str], subject_user_id: str) -> DataSubject:
        tenant_id, actor_user_id, access_token = auth
        return cls(tenant_id, subject_user_id, actor_user_id, access_token, admin=True)

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


async def _erase_llm_preferences(settings: Settings, subject: DataSubject) -> int:
    """Delete every LLM model preference the gateway keeps for the caller.

    The gateway lists them (``GET /user-preferences`` → ``{"items": [...]}``)
    and deletes one purpose at a time.  A purpose that is already gone (404)
    counts as deleted, so a repeat is harmless.
    """
    base = f"{settings.foundation_llm_gateway_url}/user-preferences"
    headers = subject.plain_headers()
    listing = await _call("GET", base, service=_LLM, headers=headers)
    items = listing.get("items") if isinstance(listing, dict) else None
    if not isinstance(items, list):
        raise UpstreamError(_LLM, "returned an unexpected preference listing")
    purposes: list[str] = []
    for item in items:
        purpose = item.get("purpose") if isinstance(item, dict) else None
        if not isinstance(purpose, str) or not _LLM_PURPOSE_RE.fullmatch(purpose):
            raise UpstreamError(_LLM, "returned a preference without a usable purpose")
        if purpose not in purposes:
            purposes.append(purpose)
    for purpose in purposes:
        try:
            resp = await _send("DELETE", f"{base}/{purpose}", headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise UpstreamError(_LLM, f"unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 400 and resp.status_code != 404:
            raise UpstreamError(_LLM, f"returned HTTP {resp.status_code}")
    return len(purposes)


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────

def _memories_url(settings: Settings, subject: DataSubject) -> str:
    if subject.admin:
        return f"{settings.knowledge_ingest_url}/internal/privacy/users/{subject.user_id}/memories"
    return f"{settings.knowledge_ingest_url}/memories"


async def build_export(settings: Settings, subject: DataSubject) -> dict[str, Any]:
    """Everything the platform holds about ``subject``, as one JSON document.

    The orchestrator, knowledge-ingest and cron-runner parts are required —
    an export that silently missed them would misstate what is held — so their
    failure raises ``UpstreamError``; ``schedules`` is ``null`` only where
    cron-runner is not deployed.  The identity profile (and, self-service only,
    the LLM model preferences the gateway keeps) are best-effort and ``null``
    when unavailable.  The admin path has no gateway lookup: the gateway only
    answers for the calling user.
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
    profile = await _call_soft(
        "GET", profile_url, service="identity", headers=subject.plain_headers(),
    )

    rows = await _bff_rows(subject)
    preferences: dict[str, Any] = {"notifications": rows["notification_preferences"]}
    if not subject.admin:
        preferences["llm"] = await _call_soft(
            "GET",
            f"{settings.foundation_llm_gateway_url}/user-preferences",
            service="llm-gateway",
            headers=subject.plain_headers(),
        )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "profile": profile,
        "platform": platform,
        "memories": memories["items"],
        "schedules": schedules,
        "preferences": preferences,
        "api_keys": rows["api_keys"],
        "privacy_acknowledgements": rows["privacy_acknowledgements"],
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


def _deleted_count(body: Any, service: str = _KNOWLEDGE) -> int:
    if isinstance(body, dict) and isinstance(body.get("deleted"), int):
        return int(body["deleted"])
    raise UpstreamError(service, "returned an unexpected erasure result")


async def run_erasure(settings: Settings, subject: DataSubject) -> ErasureRun:
    """Erase ``subject``'s data, one step at a time (see ``_ERASE_STEPS``).

    Schedules go first, so none can start another run on the user's behalf
    after the platform data is gone.  Stops at the first failing step and
    raises ``ErasureIncompleteError``; every step is idempotent, so the whole
    request can simply be repeated.
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
        if subject.admin:
            # The gateway answers only for the calling user, so an admin cannot
            # clear another member's model preferences from here.
            run.skip(step, "not_supported")
        else:
            run.results[step] = {"deleted": await _erase_llm_preferences(settings, subject)}
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


def _erasure_counts(results: dict[str, Any]) -> dict[str, Any]:
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
    if "bff" in results:
        counts["bff"] = results["bff"]
    return counts


async def erase_with_audit(
    settings: Settings,
    subject: DataSubject,
    *,
    event_type: str,
    account_note: str,
) -> dict[str, Any]:
    """``run_erasure`` + an audit event either way; incomplete → HTTP 502."""
    via = "admin_dsar" if subject.admin else "self_service"
    try:
        run = await run_erasure(settings, subject)
    except ErasureIncompleteError as exc:
        payload: dict[str, Any] = {
            "via": via,
            "succeeded": exc.succeeded,
            "failed": exc.failed,
            **_erasure_counts(exc.results),
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
    payload = {"via": via, "succeeded": run.succeeded, **_erasure_counts(run.results)}
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
        DataSubject.from_auth(_auth),
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
