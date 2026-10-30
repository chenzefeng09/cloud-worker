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
  the Claude SDK worker running inside each sandbox.  Deliberately a
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

# Smallest scope set that lets the in-sandbox Claude SDK worker do useful
# work.  Expanding this list is a code change, on purpose.
#
# Security fix B7 (least privilege) — ``notification.send`` was removed: it was
# a dead capability in the sandbox token.  Nothing in the sandbox/worker path
# consumes it (no ``notification``/``notify`` caller in the worker), there
# is no tool-broker route that proxies to a notification service, and it is not
# in ``egress_policy.BROKER_LEASE_SCOPES``.  Carrying it only widened the
# token's blast radius if leaked, for zero functional benefit.
#
# ``audit.write`` was removed with the AgentScope worker: its
# ``event_audit_sink`` extension was the only in-sandbox audit caller; the
# Claude SDK worker makes no direct audit-service calls.
SANDBOX_TOKEN_SCOPES: tuple[str, ...] = (
    "llm-gateway.invoke",
)

# Comma-separated X-Service-Scopes value the scheduler injects into the sandbox
# env as FOUNDATION_SERVICE_SCOPES.  The worker reads this and forwards it as
# the ``X-Service-Scopes`` header when calling the internal endpoints on
# mcp-registry and skill-store.
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

    # IP/host of the scheduler's machine that is reachable by the cube microVM.
    # The field/env name retains "public" for deployment compatibility, but
    # CubeSandbox v0.6 can explicitly allow a private node IP in ``allowOut``.
    # Prefer that private IP when the cloud public NAT lacks hairpin support.
    # Container names remain unresolvable inside the VM, so the runtime rewrites
    # worker gateway URLs to this host's published ports. When empty, it probes
    # for the machine's public IP and finally falls back to the controller host.
    # Pin SCHEDULER_CUBE_PUBLIC_HOST explicitly in production.
    cube_public_host: str = ""

    # CubeVS egress allowlist (CIDRs) applied to broker-mode microVMs via the
    # E2B ``allowOut`` field. It may contain private CIDRs on v0.6. For a
    # single-node deployment, allow the private node IP/32 used by the rewritten
    # gateway URL; a cluster deployment may instead need the tool-broker and DNS
    # ClusterIP/32s described in deploy/cube/README.md. Comma-separated.
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
    # API key for CubeSandbox's E2B-compatible API (``X-API-Key`` header).
    # Dev one-click default is ``e2b_000000``; override via SCHEDULER_CUBE_API_KEY.
    cube_api_key: str = "e2b_000000"

    # Sandbox image — the Claude Agent SDK worker (``worker-claude-sdk``),
    # built from ``sandbox-images/claude-sdk.Dockerfile``.  It runs via its own
    # ENTRYPOINT (node); the cube runtime launches ``node /app/dist/main.js``.
    agent_image: str = "ghcr.io/agent-platform/claude-sdk-sandbox:0.1"
    # Cube v0.6 fixes memory on the template rather than accepting a per-spawn
    # resource override. A deployment may therefore point these two aliases at
    # the same OCI image with different template memory settings. Empty values
    # fall back to the single ``agent_image`` template.
    cube_claude_sdk_template_small: str = ""
    cube_claude_sdk_template_large: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
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

    # Scheduler-side cube-sandbox fail-safe reaper. CubeSandbox v0.6 normally
    # enforces ``sandbox_timeout`` through cube-lifecycle-manager and pauses an
    # idle VM. This sweep remains as a backstop for controller/CLM failures.
    #   * ``max_age_seconds`` is intentionally far above ``sandbox_timeout`` so
    #     normal CLM-managed pause/resume owns the sandbox lifecycle.
    #   * ``interval_seconds`` is how often the sweep runs.
    # Only consumed on the cube path; docker's ``reap_expired`` is a no-op.
    sandbox_reaper_max_age_seconds: int = 3600  # delete VMs older than 1 hour
    sandbox_reaper_interval_seconds: int = 120  # sweep every 2 min

    # Host-side run workspace dirs (user files) on both runtimes: the reaper
    # removes a dir this long after its result was collected, and any dir idle
    # longer than the max age — kept above the worker's 7-day HITL pause TTL
    # so a paused run keeps its files while it can still resume.
    workspace_collected_grace_seconds: int = 600
    workspace_max_age_seconds: int = 8 * 24 * 3600

    # B-M3: base URLs for the MCP registry and skill-store services.
    # These are forwarded to the sandbox as MCP_REGISTRY_URL / SKILL_STORE_URL
    # where the in-sandbox worker appends /internal/mcp-servers and
    # /internal/skills respectively.
    # Values must include the service's API version prefix.
    mcp_registry_url: str = "http://mcp-registry:8010/api/mcp/v1"
    skill_store_url: str = "http://skill-store:8011/api/skills/v1"
    # Web-fetch gateway — the sidecar that lets sandboxed agents fetch public
    # URLs.  Forwarded into the sandbox as WEB_FETCH_GATEWAY_URL so the
    # worker's web-fetch tool can POST to /api/web-fetch/v1/fetch.  Uses the
    # service name ``web-fetch-gateway`` which is routable on the ``foundation``
    # docker network shared by all platform services and sandbox containers.
    web_fetch_gateway_url: str = "http://web-fetch-gateway:8013"

    # Foundation DLP — base URL *with* API version prefix.  Forwarded into the
    # sandbox as DLP_URL so the worker can POST tool-call content to
    # ``{DLP_URL}/core/scan`` for scanning.  In broker egress mode the URL is
    # rewritten to the tool-broker /dlp façade (which allowlists /core/scan);
    # in foundation mode it is this direct service URL.
    dlp_url: str = "http://foundation-dlp:80/api/dlp/v1"

    # What the worker does when a DLP scan cannot be completed (DLP unreachable,
    # timed out, non-2xx or unparseable answer): ``closed`` denies the tool
    # call, ``open`` allows it and logs an error each time.  Forwarded into the
    # sandbox as DLP_FAILURE_MODE (see worker-claude-sdk/src/dlp.ts).
    dlp_failure_mode: Literal["open", "closed"] = "closed"

    # LLM Gateway (M1-P3) — base URL *with* API version prefix.
    # Forwarded into every sandbox as LLM_GATEWAY_URL.  The scheduler also
    # queries GET /tenant-keys?tenant_id= on this base URL to retrieve the
    # tenant's virtual key (LLM_VIRTUAL_KEY) at spawn time.
    # Gateway mode is unconditional: a missing tenant virtual key is a
    # spawn-time error and there is no direct-connect fallback.
    llm_gateway_url: str = "http://llm-gateway:8011/api/llm-gateway/v1"
    # Proves the scheduler is a SERVICE caller to the gateway, which mints and
    # revokes run-scoped keys for no one else: sent as X-Internal-Service-Token
    # with X-Actor: SERVICE, it must equal the gateway's
    # LLM_GATEWAY_INTERNAL_SERVICE_TOKEN.  Like ``scheduler_service_token`` it
    # must NOT reach the sandboxes we spawn.  Empty → the gateway refuses the
    # mint (403) and runs fall back to the long-lived tenant key.
    llm_gateway_service_token: str = ""

    # Knowledge-ingest (KB v1) - base URL *with* API version prefix.
    # Forwarded into the sandbox as KNOWLEDGE_INGEST_URL so the in-sandbox
    # worker's knowledge tool can GET {base}/search?scope=knowledge on
    # behalf of the agent.  In foundation egress mode this is the direct ki
    # URL (ki is on the foundation net); in broker mode ``egress_policy``
    # rewrites it to the tool-broker ``/knowledge/v1`` façade.  ki needs no
    # bearer/vkey - only X-Tenant-Id / X-User-Id / X-User-Clearance, which the
    # worker derives from FOUNDATION_* env vars (and the broker re-injects
    # from the signed lease in broker mode).
    knowledge_ingest_url: str = "http://knowledge-ingest:8012/api/knowledge/v1"

    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"

    # S3/MinIO — forwarded into the sandbox so the worker can download
    # user-uploaded attachments from the agent-platform bucket.
    s3_endpoint_url: str = "http://foundation-minio:9000"
    s3_bucket: str = "agent-platform"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""

    # Hot-state layer — Redis URL forwarded into each sandbox container so the
    # worker can write step/plan/cursor checkpoints for breakpoint
    # resume.  foundation-redis is on the saas-foundation-dev_foundation network;
    # sandbox containers share that network so they reach it directly (plan a).
    # Format: redis://[:password@]host:port/db  (empty = hot-state disabled)
    redis_url: str = ""

    # Foundation authz + billing — RBAC enforcement and quota checks.
    authz_url: str = "http://authz:8002/api/authz/v1"
    foundation_billing_url: str = "http://billing:8007/api/billing/v1"

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
