"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from app_registry.config import Settings
from app_registry.domain.db import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await init_db(settings)
    yield
    await close_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    app = FastAPI(title="App Registry", version="0.1.0a1", lifespan=lifespan)
    app.state.settings = settings

    install_request_context(app, service_name="app-registry", service_version="0.1.0a1")

    from app_registry.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    register_problem_details(app, internal_code="APP.INTERNAL.ERROR")

    from app_registry.api.v1.apps import router as apps_router
    from app_registry.api.v1.health import router as health_router
    from app_registry.api.v1.privacy import router as privacy_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(apps_router, prefix=settings.api_prefix)
    app.include_router(privacy_router, prefix=settings.api_prefix)
    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "app_registry.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
