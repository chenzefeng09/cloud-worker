"""FastAPI main app factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from agent_session_orchestrator.config import Settings
from agent_session_orchestrator.domain.db import close_db, init_db
from agent_session_orchestrator.observability import init_observability


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    init_observability(service_name="session-orchestrator")
    await init_db(settings)
    yield
    await close_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="Agent Session Orchestrator",
        version="0.1.0a1",
        lifespan=lifespan,
    )
    app.state.settings = settings

    install_request_context(
        app, service_name="session-orchestrator", service_version="0.1.0a1",
    )

    from agent_session_orchestrator.api.exception_handlers import register_exception_handlers
    register_exception_handlers(app)
    register_problem_details(app, internal_code="AGENT.INTERNAL.ERROR")

    from agent_session_orchestrator.api.v1.health import router as health_router
    app.include_router(health_router, prefix=settings.api_prefix)

    from agent_session_orchestrator.api.v1.sessions import router as sessions_router
    app.include_router(sessions_router, prefix=settings.api_prefix)

    from agent_session_orchestrator.api.v1.messages import router as messages_router
    app.include_router(messages_router, prefix=settings.api_prefix)

    from agent_session_orchestrator.api.v1.runs import router as runs_router
    app.include_router(runs_router, prefix=settings.api_prefix)

    return app


def main() -> None:
    """Entry point for `poetry run session-orchestrator`."""
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "agent_session_orchestrator.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
