"""Configuration for the tool broker service."""

from __future__ import annotations

from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

# Environments where a weak/placeholder lease secret is tolerated.  Anything
# else (prod/production/staging/...) must supply a strong secret or the broker
# refuses to start — a forgeable lease secret would let a sandbox mint its own
# capability leases and bypass every per-run scope/tenant boundary.
_DEV_LIKE_ENVS = frozenset({"dev", "test", "local"})

_LEASE_SECRET_PLACEHOLDER = "dev-tool-broker-lease-secret-change-me"


def _lease_secret_is_weak(secret: str) -> bool:
    """Return ``True`` when ``secret`` is empty, a placeholder, or too short."""
    value = secret or ""
    lowered = value.lower()
    return (
        not value
        or value == _LEASE_SECRET_PLACEHOLDER
        or "change-me" in lowered
        or "replace_me" in lowered
        or len(value) < 32
    )


def lease_secret_error(env_var: str) -> str:
    """Build the fail-closed error message naming ``env_var`` + how to fix it."""
    return (
        f"{env_var} must be a strong secret (>= 32 chars, no placeholder) when "
        "the service runs outside dev. Generate one with: "
        'python -c "import secrets; print(secrets.token_urlsafe(48))"'
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TOOL_BROKER_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8024
    api_prefix: str = "/api/tool-broker/v1"

    require_auth: bool = True
    require_lease: bool = True
    lease_secret: str = "dev-tool-broker-lease-secret-change-me"
    lease_clock_skew_seconds: int = 30
    proxy_timeout_seconds: float = 30.0

    llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"
    mcp_registry_url: str = "http://mcp-registry:8010/api/mcp/v1"
    skill_store_url: str = "http://skill-store:8011/api/skills/v1"
    web_fetch_gateway_url: str = "http://web-fetch-gateway:8013/api/web-fetch/v1"
    dlp_url: str = "http://foundation-dlp:80/api/dlp/v1"
    # Knowledge-ingest (KB v1) - the /knowledge/v1/{path} façade proxies here.
    # ki needs no bearer/vkey: the broker re-injects X-Tenant-Id / X-User-Id
    # (from the lease, as for every route) PLUS X-User-Clearance (also from the
    # signed lease) so a sandbox cannot inflate its clearance to read restricted
    # documents.  The route allowlists the single ``search`` upstream path.
    knowledge_ingest_url: str = "http://knowledge-ingest:8012/api/knowledge/v1"

    # --- B2: per-route upstream path allowlist -----------------------------
    # Each passthrough proxy route forwards the sandbox-chosen ``{path}`` to an
    # upstream service.  Without a constraint that is an open passthrough into
    # the upstream's entire GET/POST surface.  The toggle below lets dev/tests
    # opt out, but production keeps it on (the default) so a sandbox can reach
    # ONLY the specific upstream paths the worker's extensions actually call.
    enforce_upstream_allowlist: bool = True

    # --- /state façade (Redis) ---------------------------------------------
    # The broker's OWN trusted connection to foundation Redis.  This is never
    # exposed to the sandbox; sandboxes reach Redis only through the narrow
    # /state/v1 HTTP API which enforces the per-run lease run_id boundary.
    redis_url: str = "redis://foundation-redis:6379/0"

    # --- /s3 façade (object store) -----------------------------------------
    # The broker's OWN trusted object-store credentials.  Sandboxes reach S3
    # only through the read-only /s3/v1 façade, which enforces the lease
    # tenant namespace (``tenant/{tenant_id}/...``) on every key.
    s3_endpoint_url: str = "http://foundation-minio:9000"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "minioadmin"
    s3_secret_access_key: str = "minioadmin"
    s3_bucket: str = "agent-platform"

    def model_post_init(self, __context: Any) -> None:
        """Fail closed on a weak lease secret outside dev-like environments.

        The lease secret is the trust anchor for the per-run capability leases:
        a placeholder/weak value lets anyone forge leases and defeat every
        scope/tenant boundary.  In prod/staging we refuse to start rather than
        run forgeable.
        """
        if self.env not in _DEV_LIKE_ENVS and _lease_secret_is_weak(
            self.lease_secret
        ):
            raise ValueError(lease_secret_error("TOOL_BROKER_LEASE_SECRET"))
