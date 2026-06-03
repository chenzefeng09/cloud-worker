"""FastAPI main app factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from foundation_sdk.fastapi import install_request_context, register_problem_details

from agent_scheduler.config import Settings
from agent_scheduler.observability import init_observability


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_observability(service_name="agent-scheduler")
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

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
