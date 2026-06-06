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

from typing import Literal

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

    # Runtime backend — "docker" for dev/CI (no KVM needed), "cube" for staging/prod.
    # When set to "cube" the pool uses PoolCubeRuntime instead of PoolDockerRuntime.
    runtime: str = "docker"

    # Upstream — the actual VM controller we hand pool requests off to.
    cube_controller_url: str = "http://cube-controller:8443"

    # mTLS to the CubeSandbox controller — mirrors agent-scheduler's cube_* settings.
    # Values are read from SANDBOX_POOL_CUBE_TLS_MODE / _CLIENT_CERT / _CLIENT_KEY /
    # _CA_CERT env vars (or the compose override agent-platform.cube.yml).
    # "mtls" is the production default; "off" for dev without TLS.
    cube_tls_mode: Literal["off", "tls", "mtls"] = "mtls"
    cube_client_cert: str | None = None
    cube_client_key: str | None = None
    cube_ca_cert: str | None = None

    # Pool sizing
    idle_low_watermark: int = 5
    idle_high_watermark: int = 20
    max_total: int = 200
    acquire_timeout_seconds: int = 30

    # Image templates the pool pre-warms.  At dev time we only warm
    # "base"; production tiers add "office" / "matlab".
    warmup_templates: list[str] = ["base"]

    # Docker runtime — mirrors agent-scheduler's setup.
    # docker_host is picked up by the docker SDK via DOCKER_HOST env var when
    # empty; set explicitly here only for tests or non-standard deployments.
    docker_host: str = ""  # empty → SDK picks up DOCKER_HOST from env
    # Sandbox image to run as idle containers.
    sandbox_image: str = "ghcr.io/agent-platform/agentscope-sandbox:0.1"
    # Docker network the pool-managed containers join (must match compose network).
    docker_network: str = "saas-foundation-dev_foundation"
    # Resource limits for idle sandbox containers (mirrors scheduler defaults).
    sandbox_memory_limit: str = "2g"
    sandbox_cpu_shares: int = 1024

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
