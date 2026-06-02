"""BFF configuration."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    session_orchestrator_url: str = "http://localhost:8006/api/session/v1"

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
