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

    # Skill-store — used to resolve the originating user's installed skills
    # that are enabled by default when a sandbox run omits the ``skills`` key.
    # The service scope is sent only to skill-store's internal read endpoint.
    skill_store_url: str = "http://skill-store:8011/api/skills/v1"
    skill_store_service_scopes: str = "skill.read_internal"

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
    # Trash grace period — soft-deleted sessions (and soft-deleted workspaces /
    # canvases) whose ``deleted_at`` is older than this many days are physically
    # purged, together with their child rows and S3 blobs, by the same sweep
    # loop.  ``0`` purges on the next sweep; a negative value disables the hard
    # purge.  Env var: SESSION_ORCHESTRATOR_TRASH_GRACE_DAYS
    trash_grace_days: int = 30
    # Service accounts that start runs on a user's behalf — cron-runner's
    # scheduled runs (``CRON_RUNNER_SERVICE_USER_ID``).  Their sessions carry
    # ``metadata.on_behalf_of_user_id``, and that user's privacy export and
    # erasure include them.  The tag counts only on sessions these accounts
    # own: any user can set session metadata on their own sessions.
    # Env var (JSON list): SESSION_ORCHESTRATOR_ON_BEHALF_PRINCIPAL_USER_IDS
    on_behalf_principal_user_ids: list[str] = ["usr_01SVCCRON00000000000000000"]

    # Redis — used to read agent run hot-state (plan/step/cursor) written by the
    # Claude SDK worker inside the sandbox.  Key schema: agent:run:{run_id}:state.
    # Failure-open: if unset or unreachable, the /state endpoint returns DB status
    # with empty plan/cursor fields rather than 500.
    redis_url: str | None = None

    # Maximum size (bytes) of a single file uploaded to a workspace file library.
    workspace_file_max_bytes: int = 20 * 1024 * 1024  # 20 MiB
