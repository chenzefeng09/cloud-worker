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
        service_token=settings.service_token,
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

    # Canvas 局部修订 — 直连 llm-gateway 做一次 chat completion，不建 run/不落消息。
    from agent_bff.api.v1.canvas import router as canvas_router
    app.include_router(canvas_router, prefix=settings.api_prefix)

    # Canvas v2 作用域编辑 — LLM 边缘产出「建议」经 orchestrator 落库（建议-确认制）。
    from agent_bff.api.v1.canvas_v2 import router as canvas_v2_router
    app.include_router(canvas_v2_router, prefix=settings.api_prefix)

    # Canvas v2 CRUD/版本/建议 — 哑代理转发到 orchestrator（system of record）。
    from agent_bff.api.v1.canvas_v2_proxy import router as canvas_v2_proxy_router
    app.include_router(canvas_v2_proxy_router, prefix=settings.api_prefix)

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

    from agent_bff.api.v1.admin_sso import router as admin_sso_router
    app.include_router(admin_sso_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_billing import router as admin_billing_router
    app.include_router(admin_billing_router, prefix=settings.api_prefix)

    # LLM model management — tenant-scoped lanes (model.read / model.admin);
    # provider writes + probe stay super_admin in admin_router above.
    from agent_bff.api.v1.admin_model import router as admin_model_router
    app.include_router(admin_model_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_audit import router as admin_audit_router
    app.include_router(admin_audit_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_flags import router as admin_flags_router
    app.include_router(admin_flags_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.flags import router as flags_router
    app.include_router(flags_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_platform import router as admin_platform_router
    app.include_router(admin_platform_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_notification import (
        router as admin_notification_router,
    )
    app.include_router(admin_notification_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_api_platform import (
        router as admin_api_platform_router,
    )
    app.include_router(admin_api_platform_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_pam import router as admin_pam_router
    app.include_router(admin_pam_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.pam import router as pam_router
    app.include_router(pam_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_dlp import router as admin_dlp_router
    app.include_router(admin_dlp_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_compliance import (
        router as admin_compliance_router,
    )
    app.include_router(admin_compliance_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_security_ops import (
        router as admin_security_ops_router,
    )
    app.include_router(admin_security_ops_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_lifecycle import (
        router as admin_lifecycle_router,
    )
    app.include_router(admin_lifecycle_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_large_attachment import (
        router as admin_large_attachment_router,
    )
    app.include_router(admin_large_attachment_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_data_protection import (
        router as admin_data_protection_router,
    )
    app.include_router(admin_data_protection_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.admin_metrics import (
        router as admin_metrics_router,
    )
    app.include_router(admin_metrics_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.billing import router as billing_router
    app.include_router(billing_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.llm_preferences import router as llm_preferences_router
    app.include_router(llm_preferences_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.notifications import router as notifications_router
    app.include_router(notifications_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.api_keys import router as api_keys_router
    app.include_router(api_keys_router, prefix=settings.api_prefix)

    from agent_bff.api.v1.schedules import router as schedules_router
    app.include_router(schedules_router, prefix=settings.api_prefix)

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
