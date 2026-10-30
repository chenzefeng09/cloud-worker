"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_REGISTRY_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8013
    api_prefix: str = "/api/apps/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    # Postgres schema that owns this service's tables.  See skill-store's config
    # for the rationale (foundation per-service schema isolation; ORM stays
    # schema-less + schema_translate_map on Postgres).
    db_schema: str = "app_registry"

    # Internal scope a service account must present to call /internal/* routes
    # (mirrors skill-store's ``skill.read_internal``).  The BFF run resolver
    # forwards this when it resolves a published App harness for a run.
    internal_scope: str = "app.read_internal"

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
    # Log every SQL statement (APP_REGISTRY_SQL_ECHO=true), for a targeted SQL-debugging
    # session only; off by default, dev included. Bound parameters are never
    # logged either way: they carry tenant data.
    sql_echo: bool = False

    # Foundation authz — RBAC enforcement (fail-closed guard).
    authz_url: str = "http://authz:8002/api/authz/v1"
