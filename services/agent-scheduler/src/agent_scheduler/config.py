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

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Smallest scope set that lets the in-sandbox AgentScope worker do useful
# work: call the LLM gateway, write audit events for tool calls, and
# dispatch notifications.  Expanding this list is a code change, on purpose.
SANDBOX_TOKEN_SCOPES: tuple[str, ...] = (
    "audit.write",
    "llm-gateway.invoke",
    "notification.send",
)

# Comma-separated X-Service-Scopes value the scheduler injects into the sandbox
# env as FOUNDATION_SERVICE_SCOPES.  The mcp_tenant_injector and skills_loader
# extensions read this and forward it as the ``X-Service-Scopes`` header when
# calling the internal endpoints on mcp-registry and skill-store.
#
# In production the API gateway translates the verified Bearer JWT into this
# header before the request reaches the service; in dev (no gateway between
# the sandbox and these services) the scheduler provides it directly so the
# internal endpoints can authorise the call.
#
# Deliberately a module constant — widening this set is a security decision
# that should go through code review, not env-var twiddling.
SANDBOX_SERVICE_SCOPES: str = (
    "mcp.read_internal,skill.read_internal,web_fetch.invoke"
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

    # Keycloak fallback for bootstrapping ``scheduler_service_token``.
    # When ``scheduler_service_token`` is unset (or set to a known
    # placeholder), the scheduler will fetch a service-account token from
    # Keycloak via the OIDC client_credentials grant and cache it until
    # it nears expiry.  This unblocks the dev/CI smoke loop where there
    # is no out-of-band mechanism to install a long-lived JWT.
    keycloak_url: str = ""
    keycloak_realm: str = "foundation"
    keycloak_client_id: str = ""
    keycloak_client_secret: str = ""
    # Tenant header to forward to identity when calling
    # ``/sessions/service-account-token``.  Required because the identity
    # middleware insists on a tenant context even for service-to-service
    # calls; the value should be the platform tenant the scheduler runs
    # under (a single-tenant deploy can use the smoke tenant).
    scheduler_tenant_header: str = ""

    # Sandbox runtime — see ADR 0001 + docs/design/b-m2-sandbox-runtime-v1.md.
    # "docker" is the dev/CI default (no KVM needed); "cube" is the production
    # default; "kata" is reserved as the fallback when CubeSandbox doesn't fit.
    runtime: str = "docker"
    cube_controller_url: str = ""  # required when runtime="cube"

    # mTLS to the CubeSandbox controller — see ADR 0001 §5 "trust boundary
    # between scheduler and CubeProxy".  Modes:
    #   * "mtls" (default, production): present a client cert *and* verify
    #     the controller's cert against ``cube_ca_cert``.  All three paths
    #     must be set or the runtime refuses to build a client.
    #   * "tls" : only verify the controller's cert (no client cert).  Use
    #     this when CubeProxy is fronted by an mTLS-terminating sidecar
    #     (Istio, Linkerd) that handles client auth out of band.
    #   * "off" : plain HTTP.  Dev only — keeps integration tests cheap
    #     against a local CubeProxy stub.
    cube_tls_mode: Literal["off", "tls", "mtls"] = "mtls"
    cube_client_cert: str | None = None
    cube_client_key: str | None = None
    cube_ca_cert: str | None = None

    # Sandbox image — built from ``sandbox-images/base.Dockerfile``,
    # ships AgentScope 2.x + agentscope_ext.runtime_worker.  The
    # runtime invokes ``python -m agentscope_ext.runtime_worker`` as
    # the container/VM entrypoint.
    agent_image: str = "ghcr.io/agent-platform/agentscope-sandbox:0.1"
    llm_api_key: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
    # Optional LLM provider override for the in-sandbox worker.
    # ``llm_provider`` selects the AgentScope model adapter
    # (e.g. ``anthropic`` default, ``openai`` for OpenAI-compatible endpoints
    # like DeepSeek / Together / a local vLLM).  ``llm_base_url`` overrides the
    # provider base URL (only honoured by providers that accept it, such as
    # ``openai``).  Both are forwarded to the sandbox as ``LLM_PROVIDER`` /
    # ``LLM_BASE_URL`` env vars; the worker treats the empty string as "use
    # provider default" so a stock deploy that doesn't set them keeps working.
    llm_provider: str = ""
    llm_base_url: str = ""
    workspace_base: str = "/var/agent-platform/workspaces"
    docker_network: str = "saas-foundation-dev_foundation"

    sandbox_memory_limit: str = "2g"
    sandbox_cpu_shares: int = 1024
    sandbox_timeout: int = 600  # seconds; also caps minted token TTL

    # B-M3: base URLs for the MCP registry and skill-store services.
    # These are forwarded to the sandbox as MCP_REGISTRY_URL / SKILL_STORE_URL
    # where the in-sandbox extensions (mcp_tenant_injector, skills_loader)
    # append /internal/mcp-servers and /internal/skills respectively.
    # Values must include the service's API version prefix.
    mcp_registry_url: str = "http://mcp-registry:8010/api/mcp/v1"
    skill_store_url: str = "http://skill-store:8011/api/skills/v1"
    # Comma-separated list of agentscope_ext extensions to activate inside
    # the sandbox.  Order matters: load MCP / Skills / web tools before the
    # audit + LLM gateway hooks so every tool call and model call is captured.
    # The enterprise default is fail-closed: no direct provider keys in the
    # sandbox, and every model call must route through foundation llm-gateway.
    runtime_extensions: str = (
        "mcp_tenant_injector,skills_loader,web_fetch,"
        "event_audit_sink,llm_gateway_enforcer"
    )

    # Web-fetch gateway — the sidecar that lets sandboxed agents fetch public
    # URLs.  Forwarded into the sandbox as WEB_FETCH_GATEWAY_URL so the
    # web_fetch extension can POST to /api/web-fetch/v1/fetch.  Uses the
    # service name ``web-fetch-gateway`` which is routable on the ``foundation``
    # docker network shared by all platform services and sandbox containers.
    web_fetch_gateway_url: str = "http://web-fetch-gateway:8013"

    # LLM Gateway (M1-P3) — base URL *with* API version prefix.
    # Forwarded into every sandbox as LLM_GATEWAY_URL.  The scheduler also
    # queries GET /tenant-keys?tenant_id= on this base URL to retrieve the
    # tenant's virtual key (LLM_VIRTUAL_KEY) at spawn time.
    # The enforcer is enabled by default.  A missing tenant virtual key is a
    # spawn-time error; direct-connect is reserved for explicit local dev only.
    llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # S3/MinIO — forwarded into the sandbox so the runtime_worker can download
    # user-uploaded attachments from the agent-platform bucket.
    s3_endpoint_url: str = "http://foundation-minio:9000"
    s3_bucket: str = "agent-platform"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Hot-state layer — Redis URL forwarded into each sandbox container so the
    # runtime_worker can write step/plan/cursor checkpoints for breakpoint
    # resume.  foundation-redis is on the saas-foundation-dev_foundation network;
    # sandbox containers share that network so they reach it directly (plan a).
    # Format: redis://[:password@]host:port/db  (empty = hot-state disabled)
    redis_url: str = ""

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"
