"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CRON_RUNNER_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8013
    api_prefix: str = "/api/cron/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    # Postgres schema that owns this service's tables.
    db_schema: str = "cron_runner"

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
    # Log every SQL statement (CRON_RUNNER_SQL_ECHO=true), for a targeted SQL-debugging
    # session only; off by default, dev included. Bound parameters are never
    # logged either way: they carry tenant data.
    sql_echo: bool = False

    # BFF base URL — the scheduler fires App runs via the BFF proxy.
    bff_url: str = "http://localhost:8005"

    # Shared secret sent in X-Service-Token header to the BFF.
    # The BFF only accepts machine-path requests when this is non-empty.
    service_token: str = "dev_service_token_change_me"

    # User-id injected as X-User-Id for scheduler-fired runs.
    # Must match the platform user-id shape ``usr_[0-9A-Z]{26}``: downstream
    # RBAC guards (app-registry/orchestrator authz_guard deny point 1) reject
    # any other format with 403 BEFORE consulting authz, so a friendly name
    # like "svc-cron" can never be granted permissions.  The matching authz
    # role_binding row must use this exact principal_id.
    service_user_id: str = "usr_01SVCCRON00000000000000000"
