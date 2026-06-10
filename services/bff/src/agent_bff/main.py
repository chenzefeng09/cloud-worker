"""FastAPI main app factory for BFF."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from foundation_sdk import CookieSigner, CookieSignerSettings
from foundation_sdk.fastapi import install_request_context, register_problem_details

from agent_bff.api.middleware import SessionMiddleware
from agent_bff.config import Settings
from agent_bff.domain.db import close_db, init_db
from agent_bff.observability import init_observability


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_observability(service_name="agent-bff")
    await init_db(app.state.settings)
    yield
    await close_db()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    # CookieSigner runs its own validation (length ≥ 32, no placeholder strings)
    # and raises ValueError at startup if the secret is unsafe.  Letting that
    # propagate is the *desired* behaviour — we want the process to crash
    # rather than silently signing cookies that can be forged.
    signer = CookieSigner(
        CookieSignerSettings(
            secret_key=settings.cookie_secret,
            salt="agent-bff-session",
            max_age_seconds=settings.cookie_max_age,
        )
    )

    app = FastAPI(
        title="Agent Platform — BFF",
        version="0.1.0a1",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.cookie_signer = signer

    # CORS — order matters: must precede SessionMiddleware so OPTIONS preflight
    # never reaches cookie parsing.
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.add_middleware(
        SessionMiddleware,
        cookie_name=settings.cookie_name,
        signer=signer,
    )
    # Shared request-id + tenant header propagation; the cookie middleware
    # above overrides the tenant/user contextvars with the cookie-derived
    # values when present.  Order matters in Starlette: middlewares run
    # outermost-first on request, innermost-first on response, so adding
    # this last means it runs first on the request — populating the vars
    # with header data — then SessionMiddleware overwrites with cookie data.
    install_request_context(
        app, service_name="agent-bff", service_version="0.1.0a1",
    )

    from agent_bff.api.exception_handlers import register_exception_handlers
    register_exception_handlers(app)
    # Generic RFC 9457 fallback for anything not caught by BFFError handler.
    register_problem_details(app, internal_code="AGENT.BFF.INTERNAL")

    from agent_bff.api.v1.health import router as health_router
    app.include_router(health_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.auth import router as auth_router
    app.include_router(auth_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.sessions import router as sessions_router
    app.include_router(sessions_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.conversations import router as chat_router
    app.include_router(chat_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.library import router as library_router
    app.include_router(library_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.apps import router as apps_router
    app.include_router(apps_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.fleet import router as fleet_router
    app.include_router(fleet_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin import router as admin_router
    app.include_router(admin_router, prefix=settings.api_prefix)

    # Admin console v2 — foundation + platform governance proxies.
    from agent_bff.api.v1.admin_authz import router as admin_authz_router
    app.include_router(admin_authz_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_org import router as admin_org_router
    app.include_router(admin_org_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_identity import router as admin_identity_router
    app.include_router(admin_identity_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_billing import router as admin_billing_router
    app.include_router(admin_billing_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_audit import router as admin_audit_router
    app.include_router(admin_audit_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_flags import router as admin_flags_router
    app.include_router(admin_flags_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_platform import router as admin_platform_router
    app.include_router(admin_platform_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.billing import router as billing_router
    app.include_router(billing_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.llm_preferences import router as llm_preferences_router
    app.include_router(llm_preferences_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.notifications import router as notifications_router
    app.include_router(notifications_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.api_keys import router as api_keys_router
    app.include_router(api_keys_router, prefix=settings.api_prefix)

    return app


def main() -> None:
    import uvicorn
    settings = Settings()
    uvicorn.run(
        "agent_bff.main:create_app",
        host="0.0.0.0",
        port=settings.http_port,
        reload=settings.env == "dev",
        factory=True,
    )
