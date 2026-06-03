"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from skill_store.blob.base import BlobStore
from skill_store.blob.s3 import S3BlobStore
from skill_store.config import Settings
from skill_store.domain.db import close_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await init_db(settings)
    yield
    await close_db()


def _default_blob_store(settings: Settings) -> BlobStore:
    # Honour SKILL_STORE_BLOB_BACKEND so the dev compose can run without AWS
    # credentials.  Tests still pass an explicit InMemoryBlobStore.
    if settings.blob_backend == "memory":
        from skill_store.blob.memory import InMemoryBlobStore
        return InMemoryBlobStore()
    return S3BlobStore(
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )


def create_app(
    settings: Settings | None = None,
    *,
    blob_store: BlobStore | None = None,
) -> FastAPI:
    if settings is None:
        settings = Settings()

    app = FastAPI(title="Skill Store", version="0.1.0a1", lifespan=lifespan)
    app.state.settings = settings
    # ``blob_store`` is injectable so tests can pass an InMemoryBlobStore
    # without needing AWS credentials.  Production passes None → S3.
    app.state.blob_store = blob_store if blob_store is not None else _default_blob_store(settings)

    from skill_store.services.quota_guard import QuotaGuard
    app.state.quota_guard = QuotaGuard(billing_url=settings.foundation_billing_url)

    install_request_context(app, service_name="skill-store", service_version="0.1.0a1")

    from skill_store.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    register_problem_details(app, internal_code="SKILL.INTERNAL.ERROR")

    from skill_store.api.v1.health import router as health_router
    from skill_store.api.v1.skills import router as skills_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(skills_router, prefix=settings.api_prefix)
    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "skill_store.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
