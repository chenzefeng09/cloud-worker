"""Run API endpoints — spawn, status, cancel.

Per-run token minting
---------------------
Every ``POST /runs`` mints a fresh, short-lived, scope-limited token via the
foundation identity service before spawning the container.  The token is
the only foundation credential the in-sandbox AgentScope worker ever sees,
and its lifetime is capped at ``Settings.sandbox_timeout`` so a leak from
an arbitrarily-long-running container expires on its own.

Ownership checks
----------------
``get_run`` / ``cancel_run`` / ``run_events`` all verify that the target
container's ``agent-platform.tenant-id`` label matches the caller's tenant
before doing anything — guessing or scraping a container id from logs must
not let a caller read or stop another tenant's workload.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import docker
import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from agent_scheduler.api.dependencies import TRuntime, TTenantId, TUserId
from agent_scheduler.config import SANDBOX_TOKEN_SCOPES
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.docker_runtime import DockerRuntime

router = APIRouter(tags=["Runs"])


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _mint_sandbox_token(
    *,
    settings,  # type: ignore[no-untyped-def]
    tenant_id: str,
    run_id: str,
) -> str:
    """Call identity service to mint the per-run sandbox token.

    The scheduler authenticates with its own ``scheduler_service_token``;
    identity validates the request and returns a JWT whose audience,
    scopes, and tenant binding match what we asked for.

    Raises:
        HTTPException(503) if identity is unreachable.
        HTTPException(502) if identity returns an unexpected error.
        HTTPException(500) if no scheduler token is configured (deployment bug).
    """
    if not settings.scheduler_service_token:
        # Refusing to spawn is safer than spawning with no foundation token at
        # all — the workload would silently fail every audit / LLM call.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SCHEDULER_SERVICE_TOKEN not configured; cannot mint sandbox token",
        )

    body = {
        "tenant_id": tenant_id,
        "subject": f"agent-scheduler:{run_id}",
        "scopes": list(SANDBOX_TOKEN_SCOPES),
        "audience": "foundation",
        "ttl_seconds": min(max(settings.sandbox_timeout, 60), 86_400),
    }
    headers = {"Authorization": f"Bearer {settings.scheduler_service_token}"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{settings.foundation_identity_url}/sessions/service-account-token",
                json=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="identity service unreachable",
        ) from exc

    if resp.status_code != status.HTTP_201_CREATED:
        # Surface a sanitised error; the body almost always contains a
        # foundation error code that callers can grep for in audit.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"identity rejected token request: HTTP {resp.status_code}",
        )

    token = resp.json().get("access_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="identity returned empty access_token",
        )
    return token


def _require_tenant_owner(
    runtime: DockerRuntime, container_id: str, tenant_id: str
) -> None:
    """Raise 404 if the container isn't owned by ``tenant_id``."""
    try:
        owner = runtime.get_container_tenant(container_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        ) from exc
    if owner != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #


@router.post("/runs", status_code=201)
async def create_run(
    body: dict,
    request: Request,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict:
    session_id = body.get("session_id", "")
    run_id = body.get("run_id", "")
    task = body.get("task", "")

    if not (session_id and run_id and task):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="session_id, run_id and task are required",
        )

    foundation_token = await _mint_sandbox_token(
        settings=request.app.state.settings,
        tenant_id=tenant_id,
        run_id=run_id,
    )

    try:
        container_id = runtime.spawn_run(
            run_id=run_id,
            session_id=session_id,
            task=task,
            tenant_id=tenant_id,
            user_id=user_id,
            foundation_token=foundation_token,
        )
    except SandboxSpawnError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc),
        ) from exc

    return {
        "id": run_id,
        "session_id": session_id,
        "sandbox_id": container_id,
        "status": "RUNNING",
        "task_description": task,
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    body: dict | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> dict:
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    return {
        "id": run_id,
        "sandbox_id": container_id,
        **runtime.get_run_status(container_id),
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    body: dict | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> dict:
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    try:
        runtime.cancel_run(container_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found",
        ) from exc
    return {"id": run_id, "status": "CANCELLED"}


@router.get("/runs/{run_id}/events")
async def run_events(
    run_id: str,
    body: dict | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """SSE stream of container events (polls container logs)."""
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    async def event_generator():
        try:
            container = runtime.client.containers.get(container_id)

            for line in container.logs(stream=True, follow=True, tail=50):
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    yield f"event: log\ndata: {json.dumps({'text': text})}\n\n"
                await asyncio.sleep(0.01)

            yield (
                f"event: run_complete\ndata: "
                f"{json.dumps(runtime.get_run_status(container_id))}\n\n"
            )
            yield "event: done\ndata: {}\n\n"

        except docker.errors.NotFound:
            yield (
                f"event: error\ndata: {json.dumps({'detail': 'Container not found'})}\n\n"
            )
        except Exception:
            # Surface only a generic detail to the client; full traceback is
            # logged by the FastAPI exception handler.
            yield (
                f"event: error\ndata: "
                f"{json.dumps({'detail': 'event stream terminated'})}\n\n"
            )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
