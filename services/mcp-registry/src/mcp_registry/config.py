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
    # Log every SQL statement (MCP_REGISTRY_SQL_ECHO=true), for a targeted SQL-debugging
    # session only; off by default, dev included. Bound parameters are never
    # logged either way: they carry tenant data.
    sql_echo: bool = False

    # Foundation authz — RBAC enforcement.
    authz_url: str = "http://authz:8002/api/authz/v1"

    # Foundation crypto - envelope encryption for auth secrets
    # (token / api_key / client_secret) and every ``env`` / ``headers`` value.
    # Mirrors llm-gateway's crypto wiring; the crypto service runs on the shared
    # foundation network as ``crypto:8004``.  See ``services/secret_crypto.py``.
    crypto_url: str = "http://crypto:8004/api/crypto/v1"
    crypto_timeout: float = 5.0
    # Local development only: store a secret in plaintext when the crypto
    # service can't encrypt it.  Off by default, so a write carrying a secret
    # fails with 503 instead of putting the secret in the database in clear.
    allow_plaintext_secrets: bool = False
