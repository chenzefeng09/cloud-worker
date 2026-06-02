"""Configuration — env vars with pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SESSION_ORCHESTRATOR_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    log_format: str = "console"
    http_port: int = 8006
    api_prefix: str = "/api/session/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"

    agent_scheduler_url: str = "http://localhost:8007/api/scheduler/v1"

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # DB pool
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
