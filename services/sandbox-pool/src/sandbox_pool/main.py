"""FastAPI app factory + lifespan for sandbox-pool."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details
from prometheus_client import CollectorRegistry, make_asgi_app

from sandbox_pool.config import Settings
from sandbox_pool.logging_setup import configure_logging
from sandbox_pool.pool import Pool

logger = logging.getLogger(__name__)

# How long shutdown waits for the warm spares to stop: inside Kubernetes'
# default 30 s termination grace period.
_DRAIN_TIMEOUT_SECONDS = 20.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Spin a periodic warm-up task while the app is alive.

    The KEDA-driven operator (B-M2 follow-up) will replace this with
    event-driven scaling; the timer is just enough to make the skeleton
    do something visible in dev.
    """
    pool: Pool = app.state.pool
    settings: Settings = app.state.settings

    async def _ticker() -> None:
        while True:
            try:
                # Drop dead warm sandboxes first so this tick's warmup
                # replaces them.
                dead = await pool.reconcile_tick()
                spawned = await pool.warmup_tick()
                reaped = await pool.reap_excess_tick()
                if dead or spawned or reaped:
                    logger.info(
                        "pool tick",
                        extra={"dead": dead, "spawned": spawned, "reaped": reaped},
                    )
            except Exception:  # pragma: no cover — keep the loop alive
                logger.exception("warmup tick failed")
            await asyncio.sleep(5 if settings.env == "dev" else 30)

    orphans = await pool.reap_orphans()
    if orphans:
        logger.info("pool: stopped warm sandboxes left by an earlier run", extra={"count": orphans})
    ticker_task = asyncio.create_task(_ticker(), name="sandbox-pool-warmup")
    try:
        yield
    finally:
        ticker_task.cancel()
        # Cancelled tasks raise CancelledError on await — that's the
        # signal we wanted; suppress so shutdown completes cleanly.
        with contextlib.suppress(asyncio.CancelledError):
            await ticker_task
        # Leave no warm spares behind: the next pool process may run under a
        # new owner id (a rolling update's new pod) and would not reap them.
        try:
            drained = await asyncio.wait_for(pool.drain_idle(), timeout=_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning("pool: shutdown stopped waiting for warm sandboxes to stop")
        else:
            if drained:
                logger.info("pool: stopped warm sandboxes on shutdown", extra={"count": drained})


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    # First thing, so all later output (uvicorn's included) is JSON at the
    # configured level, with or without OTel.
    configure_logging(service="sandbox-pool", level=settings.log_level)

    app = FastAPI(
        title="Sandbox Pool",
        version="0.1.0a1",
        lifespan=lifespan,
    )
    app.state.settings = settings
    # One registry per app so create_app() can be called repeatedly (tests,
    # multi-worker setups) without "duplicated timeseries" errors.  The
    # /metrics endpoint below scrapes the same registry the pool writes to.
    registry = CollectorRegistry()
    app.state.metrics_registry = registry
    app.state.pool = Pool(settings, metrics_registry=registry)

    install_request_context(
        app, service_name="sandbox-pool", service_version="0.1.0a1",
    )
    register_problem_details(app, internal_code="AGENT.POOL.INTERNAL")

    from sandbox_pool.api.v1.health import router as health_router
    from sandbox_pool.api.v1.sandboxes import router as sandboxes_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(sandboxes_router, prefix=settings.api_prefix)

    # Prometheus scrape target — mounted at the conventional ``/metrics``
    # path (outside the API prefix so the ServiceMonitor configuration
    # in deploy/operator/README.md works without per-tier overrides).
    app.mount("/metrics", make_asgi_app(registry=registry))

    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "sandbox_pool.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
