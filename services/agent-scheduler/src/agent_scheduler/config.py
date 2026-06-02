"""Configuration — env vars with pydantic-settings.

Notes on the foundation surface
-------------------------------
* ``foundation_identity_url`` — base URL of the identity service the
  scheduler calls to mint per-run, scope-limited, tenant-bound tokens
  (``POST /sessions/service-account-token``).  See
  ``saas-foundation/docs/CONVENTIONS.md §3.7``.

* ``scheduler_service_token`` — the scheduler's own bearer token used to
  authenticate to the identity service.  This token must have *only* the
  ``identity.service-account-token.issue`` scope; it must NOT be forwarded
  to the workloads we spawn.  ``docker_runtime.spawn_run`` enforces that by
  requiring a separately-minted ``foundation_token`` argument.

* :data:`SANDBOX_TOKEN_SCOPES` — the scopes embedded in tokens we mint for
  the AgentScope worker running inside each sandbox.  Deliberately a
  **module constant, not config**: changing the scope set is a
  security-relevant decision that should go through code review, not
  env-var twiddling at deploy time.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Smallest scope set that lets the in-sandbox AgentScope worker do useful
# work: call the LLM gateway, write audit events for tool calls, and
# dispatch notifications.  Expanding this list is a code change, on purpose.
SANDBOX_TOKEN_SCOPES: tuple[str, ...] = (
    "audit.write",
    "llm-gateway.invoke",
    "notification.send",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCHEDULER_",
        env_file=".env",
        extra="ignore",
    )

    env: str = "dev"
    log_level: str = "INFO"
    log_format: str = "console"
    http_port: int = 8007
    api_prefix: str = "/api/scheduler/v1"

    # Foundation surface (see module docstring)
    foundation_api_url: str = "http://localhost:8001"
    foundation_identity_url: str = "http://localhost:8001/api/identity/v1"
    scheduler_service_token: str = ""

    # Sandbox runtime — see ADR 0001 + docs/design/b-m2-sandbox-runtime-v1.md.
    # "docker" is the dev/CI default (no KVM needed); "cube" is the production
    # default; "kata" is reserved as the fallback when CubeSandbox doesn't fit.
    runtime: str = "docker"
    cube_controller_url: str = ""  # required when runtime="cube"

    # Sandbox image — built from ``sandbox-images/base.Dockerfile``,
    # ships AgentScope 2.x + agentscope_ext.runtime_worker.  The
    # runtime invokes ``python -m agentscope_ext.runtime_worker`` as
    # the container/VM entrypoint.
    agent_image: str = "ghcr.io/agent-platform/agentscope-sandbox:0.1"
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
    workspace_base: str = "/var/agent-platform/workspaces"
    docker_network: str = "saas-foundation-dev_foundation"

    sandbox_memory_limit: str = "2g"
    sandbox_cpu_shares: int = 1024
    sandbox_timeout: int = 600  # seconds; also caps minted token TTL

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
