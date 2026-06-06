"""Configuration — env vars with pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WEB_FETCH_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    http_port: int = 8013
    api_prefix: str = "/api/web-fetch/v1"

    # Comma-separated hostnames allowed as fetch targets.
    # A value starting with "." matches that domain and all its subdomains
    # (e.g. ".wikipedia.org" matches en.wikipedia.org).
    # The special value "*" disables domain restrictions — for dev use only.
    allowed_domains: str = ""

    # Hard cap on response body size (bytes).  Per-request max_bytes is
    # further bounded to this value.
    max_response_bytes: int = 1_048_576  # 1 MiB

    fetch_timeout_seconds: float = 15.0

    # When True only text/* and application/json content-types are accepted.
    restrict_content_types: bool = True

    # The gateway is called from sandbox code, not from a trusted service mesh
    # identity today, so require the scheduler-issued bearer token and tenant
    # context by default.  JWT signature verification belongs in the shared
    # gateway/auth middleware; this check prevents accidentally exposing a
    # completely unauthenticated egress endpoint.
    require_auth: bool = True
    required_scope: str = "web_fetch.invoke"
