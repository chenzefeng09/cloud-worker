"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from knowledge_ingest.blob.base import BlobStore
from knowledge_ingest.config import Settings
from knowledge_ingest.domain.db import close_db, init_db
from knowledge_ingest.embeddings.base import EmbeddingClient
from knowledge_ingest.logging_setup import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    await init_db(settings)
    yield
    await close_db()


def _default_blob_store(settings: Settings) -> BlobStore:
    # Honour KNOWLEDGE_INGEST_BLOB_BACKEND so dev compose can run without
    # AWS credentials.  Tests pass an explicit InMemoryBlobStore.
    if settings.blob_backend == "memory":
        from knowledge_ingest.blob.memory import InMemoryBlobStore
        return InMemoryBlobStore()
    from knowledge_ingest.blob.s3 import S3BlobStore

    return S3BlobStore(
        bucket=settings.s3_bucket,
        region=settings.s3_region,
        endpoint_url=settings.s3_endpoint_url,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )


def _default_embeddings(settings: Settings) -> EmbeddingClient:
    if settings.embedding_backend == "local":
        from knowledge_ingest.embeddings.local import LocalEmbeddingClient

        return LocalEmbeddingClient(dim=settings.embedding_dim)

    from knowledge_ingest.embeddings.gateway import GatewayEmbeddingClient

    return GatewayEmbeddingClient(
        base_url=settings.foundation_llm_gateway_url,
        model=settings.embedding_model,
        timeout=settings.embeddings_timeout_seconds,
    )


def create_app(
    settings: Settings | None = None,
    *,
    blob_store: BlobStore | None = None,
    embeddings: EmbeddingClient | None = None,
) -> FastAPI:
    if settings is None:
        settings = Settings()
    # First thing, so all later output (uvicorn's included) is JSON at the
    # configured level, with or without OTel.
    configure_logging(service="knowledge-ingest", level=settings.log_level)

    app = FastAPI(title="Knowledge Ingest", version="0.1.0a1", lifespan=lifespan)
    app.state.settings = settings
    app.state.blob_store = blob_store if blob_store is not None else _default_blob_store(settings)
    app.state.embeddings = (
        embeddings if embeddings is not None else _default_embeddings(settings)
    )

    from knowledge_ingest.services.quota_guard import QuotaGuard
    app.state.quota_guard = QuotaGuard(billing_url=settings.foundation_billing_url)

    install_request_context(app, service_name="knowledge-ingest", service_version="0.1.0a1")

    from knowledge_ingest.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    register_problem_details(app, internal_code="KNOWLEDGE.INTERNAL.ERROR")

    from knowledge_ingest.api.v1.documents import router as docs_router
    from knowledge_ingest.api.v1.folders import router as folders_router
    from knowledge_ingest.api.v1.health import router as health_router
    from knowledge_ingest.api.v1.memories import router as memories_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(folders_router, prefix=settings.api_prefix)
    app.include_router(docs_router, prefix=settings.api_prefix)
    app.include_router(memories_router, prefix=settings.api_prefix)
    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "knowledge_ingest.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
