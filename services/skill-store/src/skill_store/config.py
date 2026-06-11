"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SKILL_STORE_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8011
    api_prefix: str = "/api/skills/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    # Postgres schema that owns this service's tables.  See mcp-registry's
    # config for the rationale (foundation per-service schema isolation;
    # ORM stays schema-less + schema_translate_map on Postgres).
    db_schema: str = "skill_store"

    # Blob storage — defaults are S3, swapped to in-memory in tests.
    blob_backend: str = "s3"
    s3_bucket: str = "agent-platform-skills"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None  # e.g. MinIO override
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    internal_scope: str = "skill.read_internal"
    presign_ttl_seconds: int = 600

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
    feature_flag_url: str = "http://feature-flag:8012/api/feature-flag/v1"
