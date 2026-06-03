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
from sandbox_pool.pool import Pool

logger = logging.getLogger(__name__)


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
                spawned = await pool.warmup_tick()
                reaped = await pool.reap_excess_tick()
                if spawned or reaped:
                    logger.info(
                        "pool tick",
                        extra={"spawned": spawned, "reaped": reaped},
                    )
            except Exception:  # pragma: no cover — keep the loop alive
                logger.exception("warmup tick failed")
            await asyncio.sleep(5 if settings.env == "dev" else 30)

    ticker_task = asyncio.create_task(_ticker(), name="sandbox-pool-warmup")
    try:
        yield
    finally:
        ticker_task.cancel()
        # Cancelled tasks raise CancelledError on await — that's the
        # signal we wanted; suppress so shutdown completes cleanly.
        with contextlib.suppress(asyncio.CancelledError):
            await ticker_task


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

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
