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

from typing import Any, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# Environments where a weak/placeholder broker lease secret is tolerated.
# Anything else (prod/production/staging/...) must supply a strong secret when
# the scheduler signs broker leases, or it refuses to start.
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

# Smallest scope set that lets the in-sandbox AgentScope worker do useful
# work.  Expanding this list is a code change, on purpose.
#
# Security fix B7 (least privilege) — ``notification.send`` was removed: it was
# a dead capability in the sandbox token.  Nothing in the sandbox/worker path
# consumes it (no ``notification``/``notify`` caller in agentscope_ext), there
# is no tool-broker route that proxies to a notification service, and it is not
# in ``egress_policy.BROKER_LEASE_SCOPES``.  Carrying it only widened the
# token's blast radius if leaked, for zero functional benefit.
#
# ``audit.write`` is deliberately KEPT: the ``event_audit_sink`` worker
# extension (default-enabled via ``runtime_extensions``) POSTs each agent event
# directly to ``{FOUNDATION_API_URL}/audit/v1/events`` carrying this minted
# token.  That direct call is air-gapped (and silently fails open) in ``broker``
# egress mode, but in ``foundation`` egress mode the sandbox shares the
# foundation network and the audit service genuinely enforces ``audit.write``.
# Dropping it would break audit forwarding in foundation mode, so it stays.
SANDBOX_TOKEN_SCOPES: tuple[str, ...] = (
    "audit.write",
    "llm-gateway.invoke",
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

    # CubeVS egress allowlist (CIDRs) applied to broker-mode microVMs via the
    # E2B ``allow_out`` field. Must contain the tool-broker ClusterIP/32 and the
    # cluster DNS ClusterIP/32 — resolve per cluster (kubectl get svc ap-tool-broker
    # / kube-dns) as documented in deploy/cube/README.md. Comma-separated.
    # Empty in broker mode is a deployment error (see the spawn guard): an empty
    # allow_out with allow_internet_access=false air-gaps the sandbox so it cannot
    # even reach the broker. Only consumed by the cube runtime; docker ignores it.
    cube_sandbox_allow_out: str = ""

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
    # Phase 5: alternate worker image for the new Claude SDK backend.  The
    # runtime selects between ``agent_image`` (AgentScope, default) and this
    # image per-run based on the ``enable_claude_sdk_worker`` feature flag,
    # enabling grey-release / instant rollback without a redeploy.  The Claude
    # SDK image runs via its own ENTRYPOINT (node), not the AgentScope CMD.
    claude_sdk_image: str = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"
    # Timeout (ms) for the synchronous feature-flag evaluate call on the spawn
    # path.  Kept tight: the call is failure-open, so a slow flag service must
    # not stall run scheduling.
    feature_flag_timeout_ms: int = 200
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
    # Sandbox egress policy:
    #   foundation — legacy/dev compatibility: sandbox joins the foundation net.
    #   broker     — sandbox joins only sandbox_broker_network and must use
    #                TOOL_BROKER_URL façades for LLM/MCP/WebFetch/etc.
    #   none       — Docker network is disabled; only local filesystem tools work.
    sandbox_egress_mode: Literal["foundation", "broker", "none"] = "broker"
    # FIX B6 — ``foundation`` egress joins the shared foundation network, giving
    # the sandbox direct (un-brokered) reach to internal services and, depending
    # on the network, the internet.  That "silent open" posture is fine for
    # dev/test/local but unsafe in prod, so outside dev-like envs it requires an
    # EXPLICIT opt-in via this flag (see the spawn-time guard in
    # ``DockerRuntime._assert_foundation_egress_allowed``).  Default ``broker``
    # mode is unaffected.
    allow_insecure_foundation_egress: bool = False
    sandbox_broker_network: str = "agent-platform-dev_sandbox_broker"
    tool_broker_url: str = "http://tool-broker:8024/api/tool-broker/v1"
    # HMAC secret shared with tool-broker for per-run capability leases. In
    # broker mode the scheduler signs a short-lived lease, the worker snapshots
    # it, then removes it from process env before Bash/MCP subprocesses run.
    tool_broker_lease_secret: str = "dev-tool-broker-lease-secret-change-me"

    # Stage-2 block B: content-addressed cache for materialised skill blobs.
    # The scheduler resolves a run's skill names via skill-store, downloads
    # each blob into ``{skills_cache_base}/{sha256}/`` once, and then bind-mounts
    # that path read-only into the sandbox at ``/home/skills/<name>``.
    #
    # The path must be identical inside the scheduler container and on the
    # docker daemon's host filesystem (sandboxes mount by absolute host path);
    # compose enforces this by binding the dir with the same source and target.
    skills_cache_base: str = "/var/agent-platform/skills_cache"
    # Comma-separated X-Service-Scopes value the scheduler sends to skill-store
    # when calling /internal/skills/resolve.  Only ``skill.read_internal`` is
    # required for resolve; keep this as a separate knob so an operator can add
    # scopes (e.g. read-only debugging) without rebuilding.
    skill_store_service_scopes: str = "skill.read_internal"

    sandbox_memory_limit: str = "2g"
    # Per-run sandbox memory tiers.  A run's sandbox memory limit is auto-chosen
    # from the total size of that run's attachments: ``small`` by default,
    # ``large`` once the summed attachment bytes meet/exceed the threshold.
    # The orchestrator reports ``total_attachment_bytes`` per run; the scheduler
    # owns the policy (threshold + concrete g-values) so it lives in one place.
    sandbox_memory_limit_small: str = "1g"
    sandbox_memory_limit_large: str = "4g"
    sandbox_large_attachment_threshold_bytes: int = 50 * 1024 * 1024  # 50 MiB
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
    feature_flag_url: str = "http://feature-flag:8012/api/feature-flag/v1"

    # Session-orchestrator base URL *with* API version prefix.  The scheduler
    # has no DB of its own; run history lives in the orchestrator's Postgres.
    # ``GET /runs`` proxies the orchestrator's tenant-scoped runs list endpoint
    # (GET {url}/runs) forwarding the caller's X-Tenant-Id / X-User-Id.
    session_orchestrator_url: str = "http://session-orchestrator:8006/api/session/v1"

    def model_post_init(self, __context: Any) -> None:
        """Fail closed on a weak broker lease secret in broker egress mode.

        The scheduler signs the per-run capability leases that tool-broker
        validates; a placeholder/weak ``tool_broker_lease_secret`` would let a
        sandbox forge its own leases.  Only enforced when egress actually runs
        through the broker (``sandbox_egress_mode == "broker"``) — in the other
        modes the secret is unused, so a placeholder must not block startup —
        and only outside dev-like environments.
        """
        if (
            self.sandbox_egress_mode == "broker"
            and self.env not in _DEV_LIKE_ENVS
            and _lease_secret_is_weak(self.tool_broker_lease_secret)
        ):
            raise ValueError(
                "SCHEDULER_TOOL_BROKER_LEASE_SECRET must be a strong secret "
                "(>= 32 chars, no placeholder) when the scheduler runs in "
                "broker egress mode outside dev. Generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
