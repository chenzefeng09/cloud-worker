"""APScheduler integration — fire CronJobs by calling BFF POST /apps/{id}/run.

Architecture note (single-replica caveat)
------------------------------------------
This service uses a plain in-process AsyncIOScheduler.  A second replica would
start its own scheduler instance, causing double-firing of every job.  For v1
we accept the single-replica constraint.

If multi-replica support is needed in future, the standard approach is a
distributed lock (e.g. Redis SETNX with TTL) checked inside the fire callback
before making the BFF call; the existing Redis instance in the foundation stack
is the natural candidate.  This is out of scope for v1.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Callable

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from cron_runner.config import Settings
from cron_runner.domain.db import get_session
from cron_runner.domain.models import CronJob

logger = logging.getLogger(__name__)


async def _fire_job(job_id: str, settings: Settings) -> None:
    """Execute one scheduled CronJob invocation — failure-open, never crashes the loop."""
    try:
        await _do_fire(job_id, settings)
    except Exception:
        logger.exception("unhandled exception firing cron job %s — scheduler loop continues", job_id)


async def _do_fire(job_id: str, settings: Settings) -> None:
    # 1. Re-read the CronJob row (skip if disabled/deleted).
    job: CronJob | None = None
    async for session in get_session(tenant_id="SYSTEM", user_id="svc-cron", actor="SYSTEM"):
        result = await session.execute(select(CronJob).where(CronJob.id == job_id))
        job = result.scalar_one_or_none()

    if job is None:
        logger.info("cron job %s not found at fire time — skipping", job_id)
        return

    if job.trigger_type != "schedule":
        # Webhook triggers are fired by an external HTTP call, never scheduled.
        logger.info("cron job %s is not a schedule trigger — skipping", job_id)
        return

    if not job.enabled:
        logger.info("cron job %s is disabled — skipping", job_id)
        await _persist_result(job_id, job.tenant_id, "skipped", None, settings)
        return

    # 2. GET the app via BFF to confirm it is still published.
    bff_url = settings.bff_url.rstrip("/")
    api_prefix = "/api/bff/v1"
    headers = {
        "X-Service-Token": settings.service_token,
        "X-Tenant-Id": job.tenant_id,
        "X-User-Id": settings.service_user_id,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        app_resp = await client.get(
            f"{bff_url}{api_prefix}/apps/{job.app_id}",
            headers=headers,
        )

    if app_resp.status_code == 404:
        logger.warning(
            "cron job %s: app %s not found — skipping run", job_id, job.app_id
        )
        await _persist_result(job_id, job.tenant_id, "skipped", "app not found", settings)
        return

    if app_resp.status_code >= 400:
        error = f"app fetch failed: HTTP {app_resp.status_code}"
        logger.warning("cron job %s: %s", job_id, error)
        await _persist_result(job_id, job.tenant_id, "error", error, settings)
        return

    app_data = app_resp.json()
    # Check the app has at least one published version; if no published version
    # exists run_app itself will return 409 — we surface that as "skipped".
    versions = app_data.get("versions") or []
    has_published = any(v.get("status") == "published" for v in versions)
    if not has_published:
        logger.info(
            "cron job %s: app %s has no published version — skipping", job_id, job.app_id
        )
        await _persist_result(job_id, job.tenant_id, "skipped", "no published version", settings)
        return

    # 3. POST {bff_url}/api/bff/v1/apps/{app_id}/run
    #
    # run_app parses its request body with ``await request.json()``, extracting
    # the keys: ``inputs``, ``file_inputs``, ``session_id``.
    # ``input_values`` from CronJob maps directly to ``inputs`` (flat dict of
    # field_key → scalar value; file fields are not supported from cron — no
    # attachments available).  Any extra keys in the body are silently ignored
    # by FastAPI (the endpoint does ``body.get("inputs") or {}`` rather than
    # using a strict Pydantic model).
    # ``input_values`` is stored in the frontend SchemaFormValue bucket shape
    # ``{"text": {key: str}, "multi": {key: [str]}}`` (file bucket excluded —
    # cron runs have no attachments).  run_app's ``inputs`` wants a FLAT
    # ``{field_key: value}`` dict, so flatten at this boundary; sending the
    # buckets verbatim 422s with "unknown input field 'text'/'multi'".
    stored = job.input_values or {}
    flat_inputs: dict[str, Any] = {
        **(stored.get("text") or {}),
        **(stored.get("multi") or {}),
    }
    run_body: dict[str, Any] = {
        "inputs": flat_inputs,
        "source": "schedule",  # forwarded as-is; run_app ignores unknown keys
    }

    async with httpx.AsyncClient(timeout=30) as client:
        run_resp = await client.post(
            f"{bff_url}{api_prefix}/apps/{job.app_id}/run",
            json=run_body,
            headers=headers,
        )

    if run_resp.status_code >= 400:
        error_text = f"run failed: HTTP {run_resp.status_code}"
        try:
            body = run_resp.json()
            error_text = f"run failed: HTTP {run_resp.status_code} {body}"
        except Exception:
            pass
        logger.warning("cron job %s: %s", job_id, error_text)
        await _persist_result(job_id, job.tenant_id, "error", error_text[:2000], settings)
        return

    logger.info("cron job %s: run triggered successfully", job_id)
    await _persist_result(job_id, job.tenant_id, "ok", None, settings)


async def _persist_result(
    job_id: str,
    tenant_id: str,
    status: str,
    error: str | None,
    settings: Settings,
) -> None:
    """Write last_run_at/last_status/last_error/next_run_at back to the DB."""
    try:
        async for session in get_session(tenant_id=tenant_id, user_id="svc-cron", actor="SYSTEM"):
            result = await session.execute(select(CronJob).where(CronJob.id == job_id))
            job = result.scalar_one_or_none()
            if job is not None:
                job.last_run_at = datetime.now(UTC)
                job.last_status = status
                job.last_error = error
                # Recompute next_run_at
                try:
                    trigger = CronTrigger.from_crontab(
                        job.cron_expression, timezone=job.timezone
                    )
                    job.next_run_at = trigger.get_next_fire_time(None, datetime.now(UTC))
                except Exception:
                    pass
    except Exception:
        logger.exception("failed to persist result for cron job %s", job_id)


def make_job_func(job_id: str, settings: Settings) -> Callable[[], Any]:
    """Return the coroutine function APScheduler will run for this job.

    Must be a COROUTINE function: AsyncIOScheduler routes coroutine jobs to
    its AsyncIOExecutor (awaited on the app's event loop).  A plain-sync
    wrapper would be dispatched to the default ThreadPoolExecutor instead,
    where ``asyncio.get_event_loop()`` raises under uvloop (no loop in
    worker threads) — every fire then dies with RuntimeError.
    """

    async def _run() -> None:
        await _fire_job(job_id, settings)

    return _run


async def load_all_jobs(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """On startup: load every enabled CronJob from DB and add to scheduler.

    The DB may have jobs from multiple tenants; we use tenant_id=SYSTEM to
    bypass RLS for this cross-tenant bootstrap read.
    """
    loaded = 0
    try:
        async for session in get_session(tenant_id="SYSTEM", user_id="svc-cron", actor="SYSTEM"):
            # Only schedule triggers are loaded into APScheduler; webhook
            # triggers are fired by an external HTTP call in a later phase.
            result = await session.execute(
                select(CronJob).where(
                    CronJob.enabled == True,  # noqa: E712
                    CronJob.trigger_type == "schedule",
                )
            )
            jobs = result.scalars().all()
            for job in jobs:
                try:
                    trigger = CronTrigger.from_crontab(
                        job.cron_expression, timezone=job.timezone
                    )
                    scheduler.add_job(
                        make_job_func(job.id, settings),
                        trigger=trigger,
                        id=job.id,
                        replace_existing=True,
                    )
                    loaded += 1
                except Exception:
                    logger.exception("failed to schedule job %s at startup", job.id)
    except Exception:
        logger.exception("failed to load jobs from DB at startup")
    logger.info("cron-runner: loaded %d jobs from DB", loaded)
