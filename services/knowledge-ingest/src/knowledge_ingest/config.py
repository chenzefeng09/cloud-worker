"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_INGEST_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8012
    api_prefix: str = "/api/knowledge/v1"

    db_dsn: str = "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    # Postgres schema that owns this service's tables.  See mcp-registry's
    # config for the rationale (foundation per-service schema isolation;
    # ORM stays schema-less + schema_translate_map on Postgres).
    db_schema: str = "knowledge_ingest"

    # Embeddings: foundation llm-gateway exposes /v1/embeddings (OpenAI-shape).
    foundation_llm_gateway_url: str = "http://localhost:8003/api/llm/v1"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embeddings_timeout_seconds: float = 30.0
    # "gateway" → GatewayEmbeddingClient (prod); "local" → LocalEmbeddingClient (dev/offline).
    embedding_backend: str = "gateway"

    # Blob storage for uploaded raw files.
    blob_backend: str = "s3"
    s3_bucket: str = "agent-platform-knowledge"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    chunk_size: int = 800
    chunk_overlap: int = 80

    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
