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
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import docker
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from agent_scheduler.api.authz_guard import require_permission
from agent_scheduler.api.dependencies import TRuntime, TTenantId, TUserId
from agent_scheduler.config import SANDBOX_TOKEN_SCOPES, Settings
from agent_scheduler.errors import ContainerNotFoundError, SandboxSpawnError
from agent_scheduler.services.docker_runtime import DockerRuntime

router = APIRouter(tags=["Runs"])

# Placeholder values we treat as "no token configured" so a stock dev
# compose that leaves ``SCHEDULER_SERVICE_TOKEN`` at a dummy value still
# falls through to the Keycloak bootstrap path.
_PLACEHOLDER_TOKENS = {
    "",
    "dev-noop-token-smoke-only",
    "changeme",
    "placeholder",
}

# In-memory cache of the scheduler's own bearer token when it has to be
# bootstrapped from Keycloak.  Refreshed when within ``_KC_TOKEN_REFRESH_SLACK``
# seconds of expiry.  Single-process scope is fine: scheduler runs as one
# uvicorn worker in B-M2.
_KC_TOKEN_REFRESH_SLACK = 30  # seconds
_kc_token_cache: dict[str, float | str] = {"token": "", "exp": 0.0}


async def _bootstrap_scheduler_token(settings) -> str:  # type: ignore[no-untyped-def]
    """Mint (and cache) a scheduler service-account token from Keycloak.

    Used when ``SCHEDULER_SERVICE_TOKEN`` is unset or a known placeholder.
    Uses the OIDC ``client_credentials`` grant against the scheduler's
    configured Keycloak realm.  The cached token is reused until it nears
    expiry, then refreshed.

    Raises:
        HTTPException(500) if Keycloak isn't configured (no recovery
            possible — caller must surface the misconfiguration).
        HTTPException(503) if Keycloak is unreachable.
    """
    if not (
        settings.keycloak_url
        and settings.keycloak_client_id
        and settings.keycloak_client_secret
    ):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "scheduler bearer token unavailable: neither "
                "SCHEDULER_SERVICE_TOKEN nor SCHEDULER_KEYCLOAK_* are set"
            ),
        )

    now = time.time()
    cached = str(_kc_token_cache.get("token", ""))
    exp = float(_kc_token_cache.get("exp", 0.0))
    if cached and exp - now > _KC_TOKEN_REFRESH_SLACK:
        return cached

    token_url = (
        f"{settings.keycloak_url.rstrip('/')}/realms/"
        f"{settings.keycloak_realm}/protocol/openid-connect/token"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.keycloak_client_id,
                    "client_secret": settings.keycloak_client_secret,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="keycloak unreachable while bootstrapping scheduler token",
        ) from exc

    if resp.status_code != status.HTTP_200_OK:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"keycloak rejected client_credentials: HTTP {resp.status_code}",
        )

    payload = resp.json()
    token = str(payload.get("access_token") or "")
    ttl = int(payload.get("expires_in") or 0)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="keycloak returned empty access_token",
        )

    _kc_token_cache["token"] = token
    _kc_token_cache["exp"] = now + ttl
    return token


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _mint_sandbox_token(
    *,
    settings: Settings,
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
    # Resolve the scheduler's own bearer token.  Prefer an out-of-band
    # provisioned ``SCHEDULER_SERVICE_TOKEN``; fall back to bootstrapping
    # from Keycloak when that env is unset or a known placeholder.  This
    # keeps the dev/CI smoke loop unblocked without weakening the prod
    # posture (prod still provisions a long-lived service token).
    bearer = settings.scheduler_service_token
    if bearer in _PLACEHOLDER_TOKENS:
        bearer = await _bootstrap_scheduler_token(settings)

    # Identity restricts ``subject`` to ``^[a-z0-9._:-]+$``; the orchestrator
    # generates run ids like ``run_<UUID-upper>`` which would otherwise be
    # rejected with 422.  Lower-casing is safe -- subject is an opaque tag
    # for audit, not a foreign key.
    subject_safe = f"agent-scheduler:{run_id}".lower()
    body = {
        "tenant_id": tenant_id,
        "subject": subject_safe,
        "scopes": list(SANDBOX_TOKEN_SCOPES),
        "audience": "foundation",
        "ttl_seconds": min(max(settings.sandbox_timeout, 60), 86_400),
    }
    headers = {"Authorization": f"Bearer {bearer}"}
    # Identity middleware requires a tenant header even on service-to-service
    # calls; forward the caller's tenant so the issued token is bound to the
    # right tenant (which is what the body already requests anyway).
    tenant_header = settings.scheduler_tenant_header or tenant_id
    if tenant_header:
        headers["X-Tenant-Id"] = tenant_header

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

    token: str = str(resp.json().get("access_token") or "")
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


@router.post("/runs", status_code=201,
             dependencies=[Depends(require_permission("run.create", "run"))])
async def create_run(
    body: dict[str, Any],
    request: Request,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
    user_id: str = TUserId,
) -> dict[str, Any]:
    session_id = body.get("session_id", "")
    run_id = body.get("run_id", "")
    task = body.get("task", "")
    # Optional: list of skill names to activate in the sandbox.
    # The skills_loader extension fetches their manifests from skill-store.
    skills_raw = body.get("skills")
    skills: list[str] = (
        [str(s) for s in skills_raw if s]
        if isinstance(skills_raw, list)
        else []
    )
    # Optional: list of attachment file dicts {"key", "filename", "mime"}.
    # Written to task.json; the runtime_worker downloads them to /workspace/inputs/.
    files_raw = body.get("files")
    files: list[dict[str, Any]] = (
        [f for f in files_raw if isinstance(f, dict)]
        if isinstance(files_raw, list)
        else []
    )
    # Optional: pre-fetched memory snippets from knowledge-ingest (forwarded
    # from the orchestrator's create_run).  Written to task.json["memory"] so
    # the runtime_worker can prepend them to the agent's system prompt.
    memory: str = str(body.get("memory") or "")

    if not (session_id and run_id and task):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="session_id, run_id and task are required",
        )

    quota_guard = request.app.state.quota_guard
    await quota_guard.check(tenant_id=tenant_id, dimension="agent_sessions", requested=1)

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
            skills=skills,
            files=files,
            memory=memory,
        )
    except SandboxSpawnError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc),
        ) from exc

    await quota_guard.record(
        tenant_id=tenant_id,
        dimension="agent_sessions",
        amount=1.0,
        description=f"spawn run {run_id}",
    )

    return {
        "id": run_id,
        "session_id": session_id,
        "sandbox_id": container_id,
        "status": "RUNNING",
        "task_description": task,
        "created_at": datetime.now(UTC).isoformat(),
    }


@router.get("/runs/{run_id}",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def get_run(
    run_id: str,
    body: dict[str, Any] | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    return {
        "id": run_id,
        "sandbox_id": container_id,
        **runtime.get_run_status(container_id),
    }


@router.post("/runs/{run_id}/cancel",
             dependencies=[Depends(require_permission("run.cancel", "run"))])
async def cancel_run(
    run_id: str,
    body: dict[str, Any] | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> dict[str, Any]:
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    try:
        runtime.cancel_run(container_id)
    except ContainerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="run not found",
        ) from exc
    return {"id": run_id, "status": "CANCELLED"}


@router.get("/runs/{run_id}/events",
            dependencies=[Depends(require_permission("run.read", "run"))])
async def run_events(
    run_id: str,
    body: dict[str, Any] | None = None,
    runtime: DockerRuntime = TRuntime,
    tenant_id: str = TTenantId,
) -> StreamingResponse:
    """SSE stream of container events (polls container logs)."""
    container_id = (body or {}).get("sandbox_id") or run_id
    _require_tenant_owner(runtime, container_id, tenant_id)

    async def event_generator() -> AsyncGenerator[str, None]:
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
