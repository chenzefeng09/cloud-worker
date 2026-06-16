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

    # Blob storage — S3/MinIO for session attachments.
    s3_bucket: str = "agent-platform"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    # Knowledge-ingest service — used for memory pre-fetch (read) and
    # memory write-back (after run completion).
    knowledge_ingest_url: str = "http://knowledge-ingest:8012/api/knowledge/v1"

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
    feature_flag_url: str = "http://feature-flag:8012/api/feature-flag/v1"

    # Foundation DLP — data-flow controls on the attachment path:
    #   • download check:download (fail-open on DLP error — see dlp_guard.py)
    #   • upload   scan + classification:tag (fail-open)
    # Env var: SESSION_ORCHESTRATOR_FOUNDATION_DLP_URL
    foundation_dlp_url: str = "http://foundation-dlp:80/api/dlp/v1"

    # Session-retention sweep — background job that soft-deletes sessions older
    # than ``session_retention_days``.  Env vars carry the ``SESSION_ORCHESTRATOR_``
    # prefix declared in ``model_config`` above, i.e.
    #   SESSION_ORCHESTRATOR_SESSION_RETENTION_DAYS
    #   SESSION_ORCHESTRATOR_SESSION_RETENTION_SWEEP_INTERVAL_SECONDS
    session_retention_days: int = 30
    session_retention_sweep_interval_seconds: int = 86400

    # Redis — used to read agent run hot-state (plan/step/cursor) written by the
    # runtime_worker inside the sandbox.  Key schema: agent:run:{run_id}:state.
    # Failure-open: if unset or unreachable, the /state endpoint returns DB status
    # with empty plan/cursor fields rather than 500.
    redis_url: str | None = None
