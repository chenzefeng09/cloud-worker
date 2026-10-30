"""CronJob CRUD endpoints.

GET    /cron-jobs            — list jobs for the calling tenant
POST   /cron-jobs            — create a new job
PATCH  /cron-jobs/{id}       — update fields (enabled/expression/input_values etc.)
DELETE /cron-jobs/{id}       — delete job + remove from scheduler
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cron_runner.api.dependencies import TDbSession, TTenantId, TUserId
from cron_runner.domain.models import CronJob
from cron_runner.errors import (
    CronJobNotFoundError,
    InvalidCronExpressionError,
    TriggerValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CronJobs"])

# Trigger kinds. "schedule" is the original cron behavior; "webhook" rows are
# fired by an external HTTP call in a later phase and are never scheduled here.
TRIGGER_SCHEDULE = "schedule"
TRIGGER_WEBHOOK = "webhook"
VALID_TRIGGER_TYPES = frozenset({TRIGGER_SCHEDULE, TRIGGER_WEBHOOK})

# The BFF exposes these as /triggers in a later phase; the webhook is invoked at
# this path. Surfaced to the caller so the UI can display the full webhook URL.
WEBHOOK_PATH_TEMPLATE = "/api/bff/v1/triggers/{id}/webhook"


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class CronJobCreate(BaseModel):
    app_id: str
    name: str
    # Optional now: required for trigger_type="schedule", must be absent for
    # "webhook". Enforced in the handler so we can return a clear 4xx.
    cron_expression: str | None = None
    timezone: str = "UTC"
    input_values: dict[str, Any] = {}
    enabled: bool = True
    source: str = "USER"
    trigger_type: str = TRIGGER_SCHEDULE
    # Required (non-null) for webhook triggers; opaque JSON otherwise.
    field_mapping: dict[str, Any] | None = None


class CronJobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    input_values: dict[str, Any] | None = None
    enabled: bool | None = None
    field_mapping: dict[str, Any] | None = None


class CronJobOut(BaseModel):
    id: str
    tenant_id: str
    app_id: str
    name: str
    trigger_type: str
    cron_expression: str | None
    timezone: str
    input_values: dict[str, Any]
    field_mapping: dict[str, Any] | None
    enabled: bool
    source: str
    # Who created the trigger (NULL for system-created and pre-0004 rows).
    created_by_user_id: str | None = None
    # Whether a webhook secret exists. The plaintext is NEVER exposed here.
    has_webhook_secret: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CronJobCreateOut(CronJobOut):
    """Create response. Carries the plaintext webhook secret exactly once."""

    # Present only when a webhook trigger was created (null for schedule rows).
    webhook_secret: str | None = None
    webhook_path: str | None = None


class WebhookSecretOut(BaseModel):
    """Response for secret rotation — returns the new plaintext exactly once."""

    id: str
    webhook_secret: str
    webhook_path: str


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _validate_cron(expression: str) -> None:
    """Raise InvalidCronExpressionError if expression is not a valid 5-field cron."""
    try:
        CronTrigger.from_crontab(expression)
    except Exception as exc:
        raise InvalidCronExpressionError(expression, str(exc)) from exc


def _hash_secret(plaintext: str) -> str:
    """SHA-256 hex digest — only the digest is ever persisted."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_secret() -> tuple[str, str]:
    """Return (plaintext, hash). Plaintext is returned to the caller once."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, _hash_secret(plaintext)


def _webhook_path(job_id: str) -> str:
    return WEBHOOK_PATH_TEMPLATE.format(id=job_id)


def _creator(user_id: str) -> str | None:
    """The owner to record for a new trigger: the calling user, never "SYSTEM"."""
    if not user_id or user_id == "SYSTEM" or len(user_id) > 40:
        return None
    return user_id


def _compute_next_run(expression: str, tz: str) -> datetime | None:
    try:
        trigger = CronTrigger.from_crontab(expression, timezone=tz)
        nxt = trigger.get_next_fire_time(None, datetime.now(UTC))
        return nxt
    except Exception:
        return None


def _get_scheduler(request: Request):  # type: ignore[no-untyped-def]
    """Return the APScheduler instance attached to app state (may be None in tests)."""
    return getattr(request.app.state, "scheduler", None)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@router.get("/cron-jobs", response_model=list[CronJobOut])
async def list_cron_jobs(
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> list[CronJobOut]:
    """List all CronJobs for the calling tenant."""
    result = await db.execute(
        select(CronJob).where(CronJob.tenant_id == tenant_id).order_by(CronJob.created_at)
    )
    jobs = result.scalars().all()
    return [CronJobOut.model_validate(j) for j in jobs]


@router.post("/cron-jobs", response_model=CronJobCreateOut, status_code=status.HTTP_201_CREATED)
async def create_cron_job(
    body: CronJobCreate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> CronJobCreateOut:
    """Create a new trigger.

    trigger_type="schedule" requires a valid cron_expression and is scheduled;
    trigger_type="webhook" requires field_mapping, generates a secret, and is
    NOT scheduled. The plaintext secret is returned exactly once.
    """
    trigger_type = body.trigger_type or TRIGGER_SCHEDULE
    if trigger_type not in VALID_TRIGGER_TYPES:
        raise TriggerValidationError(
            f"Unknown trigger_type {trigger_type!r}; "
            f"expected one of {sorted(VALID_TRIGGER_TYPES)}"
        )

    cron_expression: str | None = None
    next_run_at: datetime | None = None
    plaintext_secret: str | None = None
    webhook_secret_hash: str | None = None

    if trigger_type == TRIGGER_SCHEDULE:
        if not (body.cron_expression and body.cron_expression.strip()):
            raise TriggerValidationError(
                "cron_expression is required for a schedule trigger"
            )
        _validate_cron(body.cron_expression)
        cron_expression = body.cron_expression
        next_run_at = _compute_next_run(body.cron_expression, body.timezone)
    else:  # webhook
        if body.field_mapping is None:
            raise TriggerValidationError(
                "field_mapping is required for a webhook trigger"
            )
        plaintext_secret, webhook_secret_hash = _generate_secret()

    job = CronJob(
        tenant_id=tenant_id,
        app_id=body.app_id,
        name=body.name,
        trigger_type=trigger_type,
        cron_expression=cron_expression,
        timezone=body.timezone,
        input_values=body.input_values,
        field_mapping=body.field_mapping,
        webhook_secret_hash=webhook_secret_hash,
        enabled=body.enabled,
        source=body.source,
        created_by_user_id=_creator(user_id),
        next_run_at=next_run_at,
    )
    db.add(job)
    await db.flush()

    # Only schedule rows are ever added to APScheduler.
    if trigger_type == TRIGGER_SCHEDULE and body.enabled:
        scheduler = _get_scheduler(request)
        if scheduler is not None:
            _schedule_job(scheduler, job, request.app.state.settings)

    await db.refresh(job)
    out = CronJobCreateOut.model_validate(job)
    if plaintext_secret is not None:
        out.webhook_secret = plaintext_secret
        out.webhook_path = _webhook_path(job.id)
    return out


@router.patch("/cron-jobs/{job_id}", response_model=CronJobOut)
async def update_cron_job(
    job_id: str,
    body: CronJobUpdate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CronJobOut:
    """Update a CronJob's fields and reschedule if needed."""
    result = await db.execute(
        select(CronJob).where(CronJob.id == job_id, CronJob.tenant_id == tenant_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise CronJobNotFoundError(job_id)

    if body.cron_expression is not None:
        _validate_cron(body.cron_expression)
        job.cron_expression = body.cron_expression

    if body.name is not None:
        job.name = body.name
    if body.timezone is not None:
        job.timezone = body.timezone
    if body.input_values is not None:
        job.input_values = body.input_values
    if body.field_mapping is not None:
        job.field_mapping = body.field_mapping
    if body.enabled is not None:
        job.enabled = body.enabled

    scheduler = _get_scheduler(request)
    if job.trigger_type == TRIGGER_SCHEDULE and job.cron_expression:
        # Recompute next_run_at based on possibly-updated expression + tz
        job.next_run_at = _compute_next_run(job.cron_expression, job.timezone)
        if scheduler is not None:
            _reschedule_job(scheduler, job, request.app.state.settings)
    else:
        # webhook (or a schedule row with no expression) is never scheduled.
        job.next_run_at = None
        if scheduler is not None:
            _remove_job(scheduler, job.id)

    await db.flush()
    await db.refresh(job)
    return CronJobOut.model_validate(job)


@router.post("/cron-jobs/{job_id}/rotate-secret", response_model=WebhookSecretOut)
async def rotate_webhook_secret(
    job_id: str,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> WebhookSecretOut:
    """Regenerate a webhook trigger's secret and return the new plaintext once."""
    result = await db.execute(
        select(CronJob).where(CronJob.id == job_id, CronJob.tenant_id == tenant_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise CronJobNotFoundError(job_id)
    if job.trigger_type != TRIGGER_WEBHOOK:
        raise TriggerValidationError(
            "rotate-secret is only valid for a webhook trigger"
        )

    plaintext, secret_hash = _generate_secret()
    job.webhook_secret_hash = secret_hash
    await db.flush()
    return WebhookSecretOut(
        id=job.id,
        webhook_secret=plaintext,
        webhook_path=_webhook_path(job.id),
    )


@router.delete("/cron-jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_cron_job(
    job_id: str,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> None:
    """Delete a CronJob and remove it from the scheduler."""
    result = await db.execute(
        select(CronJob).where(CronJob.id == job_id, CronJob.tenant_id == tenant_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise CronJobNotFoundError(job_id)

    scheduler = _get_scheduler(request)
    if scheduler is not None:
        _remove_job(scheduler, job_id)

    await db.delete(job)


# --------------------------------------------------------------------------- #
# Scheduler helpers (thin wrappers — scheduler_service.py owns the fire logic)
# --------------------------------------------------------------------------- #

def _schedule_job(scheduler: Any, job: CronJob, settings: Any) -> None:
    from cron_runner.scheduler_service import make_job_func

    try:
        trigger = CronTrigger.from_crontab(job.cron_expression, timezone=job.timezone)
        scheduler.add_job(
            make_job_func(job.id, settings),
            trigger=trigger,
            id=job.id,
            replace_existing=True,
        )
        logger.info("scheduled cron job %s (%s)", job.id, job.cron_expression)
    except Exception:
        logger.exception("failed to schedule job %s", job.id)


def _reschedule_job(scheduler: Any, job: CronJob, settings: Any) -> None:
    _remove_job(scheduler, job.id)
    if job.enabled:
        _schedule_job(scheduler, job, settings)


def _remove_job(scheduler: Any, job_id: str) -> None:
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass  # Job may not exist in the scheduler (OK)
