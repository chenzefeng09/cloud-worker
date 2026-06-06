"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_REGISTRY_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8010
    api_prefix: str = "/api/mcp/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    # Postgres schema that owns this service's tables.  Foundation isolates
    # every service in its own schema (app_migrator owns it; public is not
    # writable by app_migrator).  The ORM models are schema-less so SQLite
    # unit tests keep working; on Postgres we translate the default schema
    # (None) to ``db_schema`` via SQLAlchemy ``schema_translate_map`` and the
    # Alembic migrations create + target the same schema.
    db_schema: str = "mcp_registry"

    # Internal scope required for cross-tenant reads (mcp_tenant_injector).
    internal_scope: str = "mcp.read_internal"

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800

    # Foundation authz — RBAC enforcement.
    authz_url: str = "http://authz:8002/api/authz/v1"
