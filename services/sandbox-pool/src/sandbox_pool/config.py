"""Pool configuration.

The four watermarks define the pool's behaviour:

* ``idle_low_watermark`` — drop below this and the pool spawns more VMs
  in the background (best-effort, async).  Setting this to ``0`` disables
  pre-warming — every ``acquire`` cold-spawns.  Useful for dev.
* ``idle_high_watermark`` — above this and the pool reaps idle VMs.
* ``max_total`` — hard cap on concurrent VMs; ``acquire`` returns 429
  once we'd exceed this.
* ``acquire_timeout_seconds`` — how long ``acquire`` waits for a warm VM
  before cold-spawning one synchronously.

These are deliberately *per-tier*: a small-business deployment runs with
``idle_low=2`` and a SaaS large deployment runs with ``idle_low=50``;
override via env.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_POOL_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    log_format: str = "console"
    http_port: int = 8008
    api_prefix: str = "/api/sandbox-pool/v1"

    # Upstream — the actual VM controller we hand pool requests off to.
    cube_controller_url: str = "http://cube-controller:8443"

    # Pool sizing
    idle_low_watermark: int = 5
    idle_high_watermark: int = 20
    max_total: int = 200
    acquire_timeout_seconds: int = 30

    # Image templates the pool pre-warms.  At dev time we only warm
    # "base"; production tiers add "office" / "matlab".
    warmup_templates: list[str] = ["base"]

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
