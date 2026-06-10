"""BFF configuration."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BFF_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    log_format: str = "console"
    http_port: int = 8005
    api_prefix: str = "/api/bff/v1"

    # Foundation identity service base URL — used for login (sessions),
    # me (resolve identity), logout (revoke).  This is identity-service,
    # NOT keycloak directly; identity-service proxies to keycloak internally.
    foundation_identity_url: str = "http://localhost:8001/api/identity/v1"

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_org_url: str = "http://org:8003/api/org/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
    # Audit service — added to dev.yml as port 8013 (8005 conflicts with BFF).
    foundation_audit_url: str = "http://audit:8013/api/audit/v1"
    # LLM Gateway — capability probe endpoint (POST /probe).
    foundation_llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"
    # Feature-flag service — flag catalogue + kill-switch (dev compose port 8012,
    # api_prefix=/api/feature-flag/v1).
    foundation_feature_flag_url: str = (
        "http://feature-flag:8012/api/feature-flag/v1"
    )

    session_orchestrator_url: str = "http://localhost:8006/api/session/v1"

    # Library services
    mcp_registry_url: str = "http://localhost:8010/api/mcp/v1"
    skill_store_url: str = "http://localhost:8011/api/skills/v1"
    knowledge_ingest_url: str = "http://localhost:8012/api/knowledge/v1"
    # App registry — App/AppVersion CRUD, versions, review, internal resolve.
    app_registry_url: str = "http://app-registry:8013/api/apps/v1"

    # Fleet services
    sandbox_pool_url: str = "http://localhost:8008/api/sandbox-pool/v1"
    agent_scheduler_url: str = "http://localhost:8007/api/scheduler/v1"

    # Cookie signing — see foundation_sdk.CookieSigner.  No default value:
    # forgetting to set this in any deployment beyond local dev should fail
    # loudly at startup instead of falling back to a forgeable cookie.
    # In dev you can put a random 48-char string in .env.
    cookie_secret: str = ""
    cookie_name: str = "ap_session"
    cookie_max_age: int = 86_400  # 24h
    cookie_secure: bool = False  # True in production (requires HTTPS)

    cors_origins: str = "http://localhost:5173,http://web:5173"

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # ── BFF-local DB (notification_preferences, future: api_keys) ──────────
    # The BFF stores per-user preferences in its own schema inside the shared
    # foundation Postgres instance.  Tests use SQLite in-memory.
    db_dsn: str = (
        "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    )
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    db_schema: str = "agent_bff"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
