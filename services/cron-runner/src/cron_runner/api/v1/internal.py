"""Internal (service-to-service) endpoints — NOT reachable via the BFF proxy.

The BFF's public webhook receiver (``POST /api/bff/v1/triggers/{id}/webhook``)
never sees the per-webhook secret hash — that stays inside cron-runner.  Instead
it presents the secret it received from the external caller to this endpoint,
which does a constant-time comparison against the stored SHA-256 digest and, on
success, returns the minimal trigger context the receiver needs to launch a run.

The BFF's privacy fan-out (GDPR export / erasure) lists and deletes one user's
triggers here, tenant-scoped by ``X-Tenant-Id``:

    GET    /internal/privacy/users/{user_id}/cron-jobs → {"items": [...]}
    DELETE /internal/privacy/users/{user_id}/cron-jobs → {"deleted": n}

Auth
----
cron-runner has no JWT trust chain; internal callers are authenticated by the
shared service token (``CRON_RUNNER_SERVICE_TOKEN`` == ``BFF_SERVICE_TOKEN`` in
every deployment).  The verify endpoint additionally requires a matching
``X-Service-Token`` header so it is not a bare unauthenticated secret-guessing
oracle even if the internal network boundary were breached.  The route is also
absent from the BFF's ``/schedules`` proxy, so it is only reachable
service-to-service.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Path, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from cron_runner.api.dependencies import TTenantId
from cron_runner.api.v1.cron_jobs import CronJobOut, _get_scheduler, _remove_job
from cron_runner.domain.db import get_session
from cron_runner.domain.models import CronJob

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Internal"])


class VerifyWebhookIn(BaseModel):
    secret: str


class VerifyWebhookOut(BaseModel):
    id: str
    tenant_id: str
    app_id: str
    field_mapping: dict | None
    # Fixed (from-config) App inputs in the frontend bucket shape
    # ``{"text": {...}, "multi": {...}}``. The BFF receiver flattens + seeds these
    # under the payload-parsed inputs (payload wins) so a webhook trigger can mix
    # fixed and from-event fields. Empty ``{}`` for all-from-event triggers.
    input_values: dict
    trigger_type: str
    # The trigger's owner, for recording whose behalf the run is on.
    created_by_user_id: str | None = None


class UserCronJobsOut(BaseModel):
    items: list[CronJobOut]


class DeletedOut(BaseModel):
    deleted: int


def _require_service_token(request: Request) -> None:
    """Reject the request unless it presents the shared service token.

    Constant-time compare; a missing/empty configured token means the machine
    path is disabled and every call is rejected (fail-closed).
    """
    configured: str = getattr(request.app.state.settings, "service_token", "") or ""
    presented = request.headers.get("X-Service-Token", "")
    if not configured or not presented or not hmac.compare_digest(
        configured.encode(), presented.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing service token",
        )


@router.post(
    "/internal/cron-jobs/{job_id}/verify",
    response_model=VerifyWebhookOut,
)
async def verify_webhook_secret(
    job_id: str,
    body: VerifyWebhookIn,
    request: Request,
) -> VerifyWebhookOut:
    """Verify a presented webhook secret against a trigger's stored hash.

    Looked up by global id (no tenant filter — the id + secret ARE the
    capability; the caller does not yet know the tenant).  Returns the trigger
    context on a match; 401 on mismatch, 404 when the id is unknown or the row
    is not a webhook trigger.  The secret is never logged.
    """
    _require_service_token(request)

    # Global lookup: a SYSTEM session bypasses RLS (same pattern the scheduler
    # uses for cross-tenant reads).  Buffer the row and only branch AFTER the
    # ``async for`` completes — returning/raising inside the loop body triggers
    # GeneratorExit and rolls back the (read-only) transaction.
    job: CronJob | None = None
    async for session in get_session(
        tenant_id="SYSTEM", user_id="svc-cron", actor="SYSTEM"
    ):
        result = await session.execute(select(CronJob).where(CronJob.id == job_id))
        job = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if job.trigger_type != "webhook" or not job.webhook_secret_hash:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not a webhook trigger"
        )

    computed = hashlib.sha256(body.secret.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(computed, job.webhook_secret_hash):
        logger.warning("webhook secret mismatch for trigger %s", job_id)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid secret"
        )

    return VerifyWebhookOut(
        id=job.id,
        tenant_id=job.tenant_id,
        app_id=job.app_id,
        field_mapping=job.field_mapping,
        input_values=job.input_values or {},
        trigger_type=job.trigger_type,
        created_by_user_id=job.created_by_user_id,
    )


# --------------------------------------------------------------------------- #
# Privacy fan-out — one user's triggers (GDPR Art. 15/20 export, Art. 17 erasure)
# --------------------------------------------------------------------------- #

_UserIdPath = Path(min_length=1, max_length=40)


@router.get(
    "/internal/privacy/users/{user_id}/cron-jobs",
    response_model=UserCronJobsOut,
)
async def list_user_cron_jobs(
    request: Request,
    user_id: str = _UserIdPath,
    tenant_id: str = TTenantId,
) -> UserCronJobsOut:
    """Every trigger ``user_id`` created in the tenant (secret hashes never leave)."""
    _require_service_token(request)
    jobs: list[CronJob] = []
    async for session in get_session(tenant_id=tenant_id, user_id="svc-cron", actor="SYSTEM"):
        result = await session.execute(
            select(CronJob)
            .where(CronJob.tenant_id == tenant_id, CronJob.created_by_user_id == user_id)
            .order_by(CronJob.created_at, CronJob.id)
        )
        jobs = list(result.scalars().all())
    return UserCronJobsOut(items=[CronJobOut.model_validate(job) for job in jobs])


@router.delete(
    "/internal/privacy/users/{user_id}/cron-jobs",
    response_model=DeletedOut,
)
async def delete_user_cron_jobs(
    request: Request,
    user_id: str = _UserIdPath,
    tenant_id: str = TTenantId,
) -> DeletedOut:
    """Delete every trigger ``user_id`` created in the tenant and unschedule it.

    Idempotent: a repeat finds nothing and reports ``deleted: 0``.
    """
    _require_service_token(request)
    scheduler = _get_scheduler(request)
    job_ids: list[str] = []
    async for session in get_session(tenant_id=tenant_id, user_id="svc-cron", actor="SYSTEM"):
        result = await session.execute(
            select(CronJob.id).where(
                CronJob.tenant_id == tenant_id, CronJob.created_by_user_id == user_id,
            )
        )
        job_ids = list(result.scalars().all())
        if job_ids:
            # Unschedule first (as DELETE /cron-jobs/{id} does): a fire that
            # races the delete then finds no row and skips.
            if scheduler is not None:
                for job_id in job_ids:
                    _remove_job(scheduler, job_id)
            await session.execute(delete(CronJob).where(CronJob.id.in_(job_ids)))
    if job_ids:
        logger.info(
            "privacy: deleted %d trigger(s)", len(job_ids), extra={"tenant_id": tenant_id},
        )
    return DeletedOut(deleted=len(job_ids))
