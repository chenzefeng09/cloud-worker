"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from cron_runner.config import Settings
from cron_runner.domain.db import close_db, init_db
from cron_runner.logging_setup import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await init_db(settings)

    # Start the APScheduler and load all persisted enabled jobs from DB.
    scheduler = AsyncIOScheduler()
    app.state.scheduler = scheduler
    scheduler.start()

    from cron_runner.scheduler_service import load_all_jobs
    await load_all_jobs(scheduler, settings)

    yield

    scheduler.shutdown(wait=False)
    await close_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()
    # First thing, so all later output (uvicorn's included) is JSON at the
    # configured level, with or without OTel.
    configure_logging(service="cron-runner", level=settings.log_level)

    app = FastAPI(title="Cron Runner", version="0.1.0a1", lifespan=lifespan)
    app.state.settings = settings

    from cron_runner.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)

    from cron_runner.api.v1.cron_jobs import router as cron_jobs_router
    from cron_runner.api.v1.health import router as health_router
    from cron_runner.api.v1.internal import router as internal_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(cron_jobs_router, prefix=settings.api_prefix)
    # Service-to-service only (webhook secret verification) — never proxied by
    # the BFF's public /schedules surface.
    app.include_router(internal_router, prefix=settings.api_prefix)
    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "cron_runner.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
