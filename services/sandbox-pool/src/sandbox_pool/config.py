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

    # Pool sizing (legacy single-bucket fields).
    #
    # These remain as back-compat defaults: any code (or test) that does not
    # use the per-tier config still reads these.  The warm loop, however, keys
    # off ``memory_tier_watermarks`` below so each memory bucket warms/reaps
    # independently.  When a tier is missing from ``memory_tier_watermarks``
    # the pool falls back to these single-bucket values for that tier.
    idle_low_watermark: int = 5
    idle_high_watermark: int = 20
    max_total: int = 200
    acquire_timeout_seconds: int = 30

    # Per-memory-tier watermarks.
    #
    # Under the Cube/microVM runtime each warm sandbox PRE-ALLOCATES its RAM,
    # so a "large" (4g) warm VM cannot serve a "small" (1g) run and vice-versa.
    # Each tier is therefore tracked, warmed and reaped as its own bucket.
    #
    # Shape: ``{tier: {"idle_low": int, "idle_high": int, "max_total": int}}``.
    # We chose a nested dict (not flat ``idle_low_small`` / ``idle_low_large``
    # fields) because:
    #   * tiers are an open set — adding a future "xlarge" bucket is a config
    #     change, not a code change (no new Settings fields);
    #   * pydantic-settings already parses complex-typed env vars as JSON, so
    #     ``SANDBOX_POOL_MEMORY_TIER_WATERMARKS='{"small":{...},"large":{...}}'``
    #     overrides the whole map the same way ``WARMUP_TEMPLATES`` (a list) is
    #     overridable — it matches the existing complex-settings convention.
    memory_tier_watermarks: dict[str, dict[str, int]] = {
        "small": {"idle_low": 2, "idle_high": 10, "max_total": 100},
        "large": {"idle_low": 1, "idle_high": 5, "max_total": 50},
    }

    # Per-tier memory sizes the pool warms each bucket at (docker-style strings).
    # "small" warms 1g VMs, "large" warms 4g VMs.  A warm sandbox spawned for a
    # tier MUST be sized for that tier or it cannot serve that tier's runs.
    memory_tier_sizes: dict[str, str] = {"small": "1g", "large": "4g"}

    # Image templates the pool pre-warms.  At dev time we only warm
    # "base"; production tiers add "office" / "matlab".
    warmup_templates: list[str] = ["base"]

    # Docker runtime — mirrors agent-scheduler's setup.
    # docker_host is picked up by the docker SDK via DOCKER_HOST env var when
    # empty; set explicitly here only for tests or non-standard deployments.
    docker_host: str = ""  # empty → SDK picks up DOCKER_HOST from env
    # Sandbox image to run as idle containers — the Claude Agent SDK worker
    # image (``worker-claude-sdk``); matches agent-scheduler's ``agent_image``.
    sandbox_image: str = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"
    # Docker network the pool-managed containers join (must match compose network).
    # Prewarmed sandboxes must NOT sit on the flat foundation network: like
    # scheduler-spawned sandboxes in broker egress mode, they join the internal
    # sandbox_broker network and reach platform capabilities via tool-broker
    # façades only. Name is the compose-project-prefixed network
    # (project ``agent-platform-dev`` + network key ``sandbox_broker``) and must
    # match the scheduler's ``sandbox_broker_network`` default.
    docker_network: str = "agent-platform-dev_sandbox_broker"
    # Resource limits for idle sandbox containers (mirrors scheduler defaults).
    sandbox_memory_limit: str = "2g"
    sandbox_cpu_shares: int = 1024

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # ---------------------------------------------------------------- tiers
    @property
    def memory_tiers(self) -> list[str]:
        """Tiers the pool tracks, derived from the per-tier watermark config.

        Falls back to ``["small"]`` if nothing is configured so back-compat
        callers (no tier passed) keep working against a single bucket.
        """
        return list(self.memory_tier_watermarks) or ["small"]

    def tier_idle_low(self, tier: str) -> int:
        cfg = self.memory_tier_watermarks.get(tier)
        return cfg["idle_low"] if cfg and "idle_low" in cfg else self.idle_low_watermark

    def tier_idle_high(self, tier: str) -> int:
        cfg = self.memory_tier_watermarks.get(tier)
        return cfg["idle_high"] if cfg and "idle_high" in cfg else self.idle_high_watermark

    def tier_max_total(self, tier: str) -> int:
        cfg = self.memory_tier_watermarks.get(tier)
        return cfg["max_total"] if cfg and "max_total" in cfg else self.max_total

    def tier_memory_size(self, tier: str) -> str:
        """Docker-style memory string the pool warms this tier at."""
        return self.memory_tier_sizes.get(tier, self.sandbox_memory_limit)
