"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from web_fetch_gateway.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="Web Fetch Gateway",
        version="0.1.0a1",
        description=(
            "Controlled outbound HTTP fetch gateway with SSRF protection for agent tools."
        ),
    )
    app.state.settings = settings

    from web_fetch_gateway.api.exception_handlers import register_exception_handlers

    register_exception_handlers(app)

    from web_fetch_gateway.api.v1.health import router as health_router
    from web_fetch_gateway.api.v1.fetch import router as fetch_router

    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(fetch_router, prefix=settings.api_prefix)

    return app


def main() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "web_fetch_gateway.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
