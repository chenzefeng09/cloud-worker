"""CronJob CRUD endpoints.

GET    /cron-jobs            — list jobs for the calling tenant
POST   /cron-jobs            — create a new job
PATCH  /cron-jobs/{id}       — update fields (enabled/expression/input_values etc.)
DELETE /cron-jobs/{id}       — delete job + remove from scheduler
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cron_runner.api.dependencies import TDbSession, TTenantId
from cron_runner.domain.models import CronJob
from cron_runner.errors import CronJobNotFoundError, InvalidCronExpressionError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["CronJobs"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class CronJobCreate(BaseModel):
    app_id: str
    name: str
    cron_expression: str
    timezone: str = "Asia/Shanghai"
    input_values: dict[str, Any] = {}
    enabled: bool = True
    source: str = "USER"


class CronJobUpdate(BaseModel):
    name: str | None = None
    cron_expression: str | None = None
    timezone: str | None = None
    input_values: dict[str, Any] | None = None
    enabled: bool | None = None


class CronJobOut(BaseModel):
    id: str
    tenant_id: str
    app_id: str
    name: str
    cron_expression: str
    timezone: str
    input_values: dict[str, Any]
    enabled: bool
    source: str
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _validate_cron(expression: str) -> None:
    """Raise InvalidCronExpressionError if expression is not a valid 5-field cron."""
    try:
        CronTrigger.from_crontab(expression)
    except Exception as exc:
        raise InvalidCronExpressionError(expression, str(exc)) from exc


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


@router.post("/cron-jobs", response_model=CronJobOut, status_code=status.HTTP_201_CREATED)
async def create_cron_job(
    body: CronJobCreate,
    request: Request,
    db: AsyncSession = TDbSession,
    tenant_id: str = TTenantId,
) -> CronJobOut:
    """Create a new CronJob and schedule it."""
    _validate_cron(body.cron_expression)

    job = CronJob(
        tenant_id=tenant_id,
        app_id=body.app_id,
        name=body.name,
        cron_expression=body.cron_expression,
        timezone=body.timezone,
        input_values=body.input_values,
        enabled=body.enabled,
        source=body.source,
        next_run_at=_compute_next_run(body.cron_expression, body.timezone),
    )
    db.add(job)
    await db.flush()

    if body.enabled:
        scheduler = _get_scheduler(request)
        if scheduler is not None:
            _schedule_job(scheduler, job, request.app.state.settings)

    await db.refresh(job)
    return CronJobOut.model_validate(job)


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
    if body.enabled is not None:
        job.enabled = body.enabled

    # Recompute next_run_at based on possibly-updated expression + tz
    job.next_run_at = _compute_next_run(job.cron_expression, job.timezone)

    scheduler = _get_scheduler(request)
    if scheduler is not None:
        _reschedule_job(scheduler, job, request.app.state.settings)

    await db.flush()
    await db.refresh(job)
    return CronJobOut.model_validate(job)


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
