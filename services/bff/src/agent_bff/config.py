"""BFF configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PrivacyNoticePurpose(BaseModel):
    """One processing purpose and its GDPR Art. 6 legal basis."""

    purpose: str
    legal_basis: str


class PrivacyNoticeProcessor(BaseModel):
    """One processor (or sub-processor) that receives personal data."""

    name: str
    role: str
    country: str
    transfer_mechanism: str


# Defaults describe what this platform does out of the box.  Operators replace
# them (JSON env vars) with their real purposes and the processors they have
# configured — the defaults deliberately name no vendor.
_DEFAULT_PRIVACY_PURPOSES: tuple[PrivacyNoticePurpose, ...] = (
    PrivacyNoticePurpose(
        purpose=(
            "Provide the agent workspace: run your conversations and tasks and "
            "store your files, workspaces and generated artifacts"
        ),
        legal_basis="Performance of a contract (GDPR Art. 6(1)(b))",
    ),
    PrivacyNoticePurpose(
        purpose=(
            "Session memory: keep short summaries of past conversations to "
            "personalise later answers (you can switch this off)"
        ),
        legal_basis=(
            "Legitimate interests (GDPR Art. 6(1)(f)); you can object at any "
            "time by turning memory off"
        ),
    ),
    PrivacyNoticePurpose(
        purpose=(
            "Security, abuse prevention and audit, including administrator "
            "review of conversations"
        ),
        legal_basis="Legitimate interests (GDPR Art. 6(1)(f))",
    ),
    PrivacyNoticePurpose(
        purpose="Usage metering, quotas and billing",
        legal_basis="Performance of a contract (GDPR Art. 6(1)(b))",
    ),
    PrivacyNoticePurpose(
        purpose="Answer data protection requests and keep the records the law requires",
        legal_basis="Legal obligation (GDPR Art. 6(1)(c))",
    ),
)

_DEFAULT_PRIVACY_PROCESSORS: tuple[PrivacyNoticeProcessor, ...] = (
    PrivacyNoticeProcessor(
        name="Configured LLM provider",
        role=(
            "Generates agent responses from your prompts, files and conversation "
            "context, reached through the platform's LLM gateway"
        ),
        country="Not specified",
        transfer_mechanism="Not specified",
    ),
    PrivacyNoticeProcessor(
        name="Configured embeddings provider",
        role="Turns documents and session memory into vectors for search",
        country="Not specified",
        transfer_mechanism="Not specified",
    ),
    PrivacyNoticeProcessor(
        name="Configured object storage",
        role="Stores uploaded files, knowledge documents and generated artifacts",
        country="Not specified",
        transfer_mechanism="Not specified",
    ),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BFF_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    log_format: str = "console"
    http_port: int = 8005
    api_prefix: str = "/api/bff/v1"

    # Foundation identity service base URL — used for login (sessions),
    # me (resolve identity), logout (revoke).  This is identity-service,
    # NOT keycloak directly; identity-service proxies to keycloak internally.
    foundation_identity_url: str = "http://localhost:8001/api/identity/v1"

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_org_url: str = "http://org:8003/api/org/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
    # Audit service — added to dev.yml as port 8013 (8005 conflicts with BFF).
    foundation_audit_url: str = "http://audit:8013/api/audit/v1"
    # LLM Gateway — capability probe endpoint (POST /probe).
    foundation_llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"
    # Optional cheap model dedicated to automatic session titles.  When empty,
    # the model selected for the first chat turn wins, then the tenant default.
    session_title_model: str = ""
    # Feature-flag service — flag catalogue + kill-switch (dev compose port 8012,
    # api_prefix=/api/feature-flag/v1).
    foundation_feature_flag_url: str = (
        "http://feature-flag:8012/api/feature-flag/v1"
    )
    # Notification service — templates + send (single / batch).
    foundation_notification_url: str = (
        "http://localhost:18008/api/notification/v1"
    )
    # API-platform — API keys, OAuth apps, webhooks.
    foundation_api_platform_url: str = (
        "http://localhost:18010/api/api-platform/v1"
    )
    # PAM — privileged-access elevations + sessions.
    foundation_pam_url: str = "http://localhost:18015/api/pam/v1"
    # DLP — data-loss-prevention classification / checks / scan.
    foundation_dlp_url: str = "http://localhost:18007/api/dlp/v1"
    # Compliance — ROPA / DPIA / DSAR / reports.
    foundation_compliance_url: str = (
        "http://localhost:18014/api/compliance/v1"
    )
    # Security-ops — vulnerabilities / incidents / scans / SBOM / drills.
    foundation_security_ops_url: str = (
        "http://localhost:18019/api/security-ops/v1"
    )
    # Lifecycle — recycle-bin / sweep / legal-holds.
    foundation_lifecycle_url: str = (
        "http://localhost:18006/api/lifecycle/v1"
    )
    # Data-protection — masking / residency / cross-border / destruction / consent.
    foundation_data_protection_url: str = (
        "http://localhost:18016/api/data-protection/v1"
    )

    # Prometheus — admin storage trend charts query Prometheus via the BFF
    # (so the browser does not need cross-origin access).  Defaults to the
    # dev compose hostname; override in production to the in-cluster URL.
    prometheus_url: str = "http://prometheus:9090"

    session_orchestrator_url: str = "http://localhost:8006/api/session/v1"

    # Large-attachment gating threshold (bytes).  A selected attachment at or
    # above this size triggers the large-memory (4g) sandbox tier and is gated
    # by the lifecycle ``large-attachment`` entitlement.  The frontend pre-check
    # (GET /attachments/large-check) uses this to decide whether to consult the
    # entitlement before uploading.
    #
    # KNOWN COUPLING: this default (50 MiB) is duplicated from the scheduler's
    # ``sandbox_large_attachment_threshold_bytes`` (agent-scheduler config).
    # Both must stay in sync; the scheduler remains the authoritative enforcer
    # of the memory tier — this value only drives the UX pre-check.
    large_attachment_threshold_bytes: int = 50 * 1024 * 1024

    # Library services
    mcp_registry_url: str = "http://localhost:8010/api/mcp/v1"
    skill_store_url: str = "http://localhost:8011/api/skills/v1"
    knowledge_ingest_url: str = "http://localhost:8012/api/knowledge/v1"
    # App registry — App/AppVersion CRUD, versions, review, internal resolve.
    app_registry_url: str = "http://app-registry:8013/api/apps/v1"

    # Fleet services
    sandbox_pool_url: str = "http://localhost:8008/api/sandbox-pool/v1"
    agent_scheduler_url: str = "http://localhost:8007/api/scheduler/v1"

    # Cookie signing — see foundation_sdk.CookieSigner.  No default value:
    # forgetting to set this in any deployment beyond local dev should fail
    # loudly at startup instead of falling back to a forgeable cookie.
    # In dev you can put a random 48-char string in .env.
    cookie_secret: str = ""
    cookie_name: str = "ap_session"
    cookie_max_age: int = 86_400  # 24h
    cookie_secure: bool = False  # True in production (requires HTTPS)

    cors_origins: str = "http://localhost:5173,http://web:5173"

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # ── BFF-local DB (notification_preferences, future: api_keys) ──────────
    # The BFF stores per-user preferences in its own schema inside the shared
    # foundation Postgres instance.  Tests use SQLite in-memory.
    db_dsn: str = (
        "postgresql+asyncpg://app_user:app_user_change_me@localhost:6432/foundation"
    )
    db_dsn_migrator: str = (
        "postgresql+asyncpg://app_migrator:app_migrator_change_me@localhost:5432/foundation"
    )
    db_schema: str = "agent_bff"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800

    # ── Service-account token ────────────────────────────────────────────────
    # Shared secret used by internal services (cron-runner) to authenticate
    # without a user session cookie.  When empty/unset the machine-auth path
    # is DISABLED — requests with X-Service-Token are rejected like any other
    # unauthenticated request.
    service_token: str = ""

    # ── Cron runner — schedules proxy + webhook secret verification ─────────
    cron_runner_url: str = "http://localhost:8013/api/cron/v1"

    # ── Inbound webhook receiver ─────────────────────────────────────────────
    # The public webhook receiver re-enters the BFF's own ``/apps/{id}/run``
    # endpoint over HTTP (machine/service-token path) to reuse the App run
    # resolver.  ``localhost`` reaches this same uvicorn process in-container.
    bff_self_url: str = "http://localhost:8005"
    # Machine identity the receiver acts as when launching webhook-triggered
    # runs.  Must match the cron-runner service account (``usr_[0-9A-Z]{26}``)
    # that is bound to the tenant ``member`` role in authz — it already carries
    # session.create / session.attachment.create / run.create.
    cron_service_user_id: str = "usr_01SVCCRON00000000000000000"
    # Hard cap on the inbound webhook request body (bytes) — rejects oversized
    # payloads before any parsing / upload.  25 MiB mirrors typical attachment
    # upload limits.
    webhook_max_body_bytes: int = 25 * 1024 * 1024

    # ── Privacy notice (GDPR Art. 13) — served by GET /privacy/notice ───────
    # The notice ``version`` is a short hash of the rendered content, so any
    # change here (or in the retention the orchestrator reports) asks users to
    # acknowledge again.  ``privacy_notice_version`` is an optional label
    # prefixed to that hash (e.g. "2026-10" → "2026-10.3fa9c2d1e0ab").
    privacy_notice_version: str = ""
    privacy_controller_name: str = "Your organisation"
    privacy_controller_contact_email: str = ""
    privacy_dpo_email: str | None = None
    # JSON lists, e.g. BFF_PRIVACY_NOTICE_PURPOSES='[{"purpose": "…",
    # "legal_basis": "…"}]' and BFF_PRIVACY_NOTICE_PROCESSORS='[{"name": "…",
    # "role": "…", "country": "…", "transfer_mechanism": "…"}]'.
    privacy_notice_purposes: list[PrivacyNoticePurpose] = Field(
        default_factory=lambda: list(_DEFAULT_PRIVACY_PURPOSES),
    )
    privacy_notice_processors: list[PrivacyNoticeProcessor] = Field(
        default_factory=lambda: list(_DEFAULT_PRIVACY_PROCESSORS),
    )

    # ── Erasure ledger (GDPR Art. 17 vs backups) — domain/erasure_ledger.py ──
    # Every erasure request is written here before it runs, so that after a
    # backup restore ``python -m agent_bff.erasure_replay`` can erase the
    # restored rows again.  The bucket must have Object Lock and must sit
    # outside what a restore rewinds.  No bucket = no ledger: erasures still
    # run, but a restore cannot replay them.
    privacy_ledger_s3_bucket: str = ""
    privacy_ledger_s3_region: str = "us-east-1"
    privacy_ledger_s3_endpoint_url: str = ""
    privacy_ledger_s3_access_key_id: str = ""
    privacy_ledger_s3_secret_access_key: str = ""
    # COMPLIANCE lock per entry; must outlast every backup that can still hold
    # erased data (Postgres full backups: 4 weeks, MinIO noncurrent: 90 days).
    privacy_ledger_retention_days: int = Field(default=365, ge=1)
