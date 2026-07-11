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
    # Emit SQLAlchemy engine SQL/echo logs.  Kept OFF by default even in dev:
    # ``echo=True`` fire-hoses every statement (and every bound param) into the
    # log stream, which — under load on the session SSE path — can back the
    # ``docker logs`` pipe up enough to starve the event loop and stall the
    # stream mid-flight.  Turn on deliberately (SESSION_ORCHESTRATOR_SQL_ECHO=true)
    # only for a targeted SQL-debugging session.
    sql_echo: bool = False
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

    # LLM gateway — the OpenAI-protocol proxy the orchestrator talks to directly
    # for chat-mode "inline" runs (POST {url}/chat/completions with
    # X-LLM-Virtual-Key).  Default aligns with the BFF value
    # (``services/bff/src/agent_bff/config.py``); the gateway resolves tenant +
    # actor from the X-Tenant-Id / X-User-Id / X-Actor headers, so no bearer is
    # required for the mesh call.  Env: SESSION_ORCHESTRATOR_LLM_GATEWAY_URL.
    llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"

    # Timeout (seconds) for a single inline /chat/completions stream. Reasoning
    # models can be slow; give a generous window before we abort the stream.
    inline_completion_timeout: float = 300.0
    # Timeout (seconds) for the lightweight gateway metadata calls made during an
    # inline run (mint/fetch virtual key, resolve default model, revoke key).
    inline_meta_timeout: float = 5.0
