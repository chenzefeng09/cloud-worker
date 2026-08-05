"""Internal (service-to-service) endpoints — NOT reachable via the BFF proxy.

The BFF's public webhook receiver (``POST /api/bff/v1/triggers/{id}/webhook``)
never sees the per-webhook secret hash — that stays inside cron-runner.  Instead
it presents the secret it received from the external caller to this endpoint,
which does a constant-time comparison against the stored SHA-256 digest and, on
success, returns the minimal trigger context the receiver needs to launch a run.

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

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select

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
    )
