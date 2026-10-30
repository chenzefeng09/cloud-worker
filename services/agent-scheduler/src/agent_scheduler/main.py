"""FastAPI main app factory."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from agent_scheduler.config import Settings
from agent_scheduler.logging_setup import configure_logging
from agent_scheduler.observability import init_observability

logger = logging.getLogger(__name__)


async def _sandbox_reaper_loop(app: FastAPI) -> None:
    """Periodically delete leaked sandboxes older than the configured max age.

    CubeSandbox v0.6 normally pauses idle microVMs through its lifecycle
    manager. This loop is the scheduler-side backstop if that control path
    fails. The cube runtime uses a SYNC ``httpx.Client``, so the sweep runs in a
    thread to avoid blocking the event loop. Any exception is caught and logged
    so the loop never dies.
    """
    settings: Settings = app.state.settings
    interval = settings.sandbox_reaper_interval_seconds
    max_age = settings.sandbox_reaper_max_age_seconds
    while True:
        await asyncio.sleep(interval)
        try:
            reaped = await asyncio.to_thread(
                app.state.runtime.reap_expired, max_age_seconds=max_age
            )
            if reaped > 0:
                logger.info("sandbox reaper deleted %d expired sandbox(es)", reaped)
        except Exception:  # noqa: BLE001 -- loop must never die
            logger.exception("sandbox reaper sweep failed; continuing")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_observability(service_name="agent-scheduler")
    reaper_task: asyncio.Task[None] | None = None
    if getattr(app.state.runtime, "reap_expired", None) is not None:
        reaper_task = asyncio.create_task(_sandbox_reaper_loop(app))
    else:
        logger.info("runtime has no reap_expired; sandbox reaper disabled")
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper_task


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    # First thing, so all later output (uvicorn's included) is JSON at the
    # configured level, with or without OTel.
    configure_logging(service="agent-scheduler", level=settings.log_level)

    app = FastAPI(
        title="Agent Scheduler",
        version="0.1.0a1",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Build the configured sandbox runtime once at startup; cached on
    # app.state so every request reuses the same Docker / httpx client.
    # Importing this here keeps the protocol / registry decoupled from
    # FastAPI — runtimes can be unit-tested without the app.
    from agent_scheduler.services import cube_runtime, docker_runtime  # noqa: F401  registration
    from agent_scheduler.services.runtime import build_runtime

    app.state.runtime = build_runtime(settings.runtime, settings)

    from agent_scheduler.services.quota_guard import QuotaGuard
    app.state.quota_guard = QuotaGuard(billing_url=settings.foundation_billing_url)

    install_request_context(
        app, service_name="agent-scheduler", service_version="0.1.0a1",
    )

    from agent_scheduler.api.exception_handlers import register_exception_handlers
    register_exception_handlers(app)
    register_problem_details(app, internal_code="AGENT.SCHEDULER.INTERNAL")

    from agent_scheduler.api.v1.health import router as health_router
    app.include_router(health_router, prefix=settings.api_prefix)

    from agent_scheduler.api.v1.runs import router as runs_router
    app.include_router(runs_router, prefix=settings.api_prefix)

    from agent_scheduler.api.v1.sandboxes import router as sandboxes_router
    app.include_router(sandboxes_router, prefix=settings.api_prefix)

    return app


def main() -> None:
    import uvicorn
    settings = Settings()
    uvicorn.run(
        "agent_scheduler.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
