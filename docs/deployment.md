# Deployment guide

This guide covers running Agent Platform beyond a single laptop: the three
deployment modes, building the sandbox images, configuration and secrets, and
backup/restore and load-test pointers.

Agent Platform is a downstream dependent of [SaaS Foundation][foundation]: it
imports `foundation_sdk` and calls Foundation services (identity, authz, billing,
audit, LLM gateway) at runtime. **Every deployment mode assumes a reachable
Foundation deployment** — either co-deployed or already running in the same
cluster. Cross-link: deploy and operate Foundation per its own docs at
[github.com/chenzefeng09/saas-foundation][foundation].

For a first local run, start with the [getting-started guide](./getting-started.md).

[foundation]: https://github.com/chenzefeng09/saas-foundation

---

## Deployment modes at a glance

| Mode | Sandbox runtime | Host requirement | Isolation | Use case |
|---|---|---|---|---|
| Local docker-compose (dev) | `docker` | Any host with Docker; **no KVM** | Sibling containers via docker-socket-proxy + broker egress | Development, CI, demos |
| CubeSandbox microVM (staging) | `cube` | **Linux host with `/dev/kvm`** | Per-run microVM + mTLS to controller + per-VM egress policy | Staging, isolation validation |
| Kubernetes via Helm | `docker` or `cube` | K8s 1.27+ (+ KVM nodes for `cube`) | Profile-dependent; broker URL-rewrite + (cube) controller-side per-VM policy | Production |

The active runtime is selected by `SCHEDULER_RUNTIME` (compose) /
`agentScheduler.config.runtime` (Helm). All runtimes execute the **same** OCI
sandbox image and the **same** worker task contract
(`task.json` → `result.json`, coded exit status), so switching runtimes does not
change agent behaviour.

---

## Mode 1 — Local docker-compose (development)

The default development mode. Agent sandboxes run as **sibling Docker
containers** spawned through a least-privilege `docker-socket-proxy` (the
scheduler never bind-mounts `/var/run/docker.sock`; `exec`, `volumes`, and
`privileged` verbs are denied). No KVM is needed.

```bash
# foundation stack + agent services (SCHEDULER_RUNTIME=docker by default)
make up
make migrate
```

Default sandbox egress is **broker** mode: sandboxes join only the internal
`sandbox_broker` network and reach infrastructure exclusively through the
tool-broker façade; the web-fetch-gateway is the only service with outbound
internet. Full step-by-step instructions and the ports reference are in the
[getting-started guide](./getting-started.md).

---

## Mode 2 — CubeSandbox microVMs (staging)

For staging on a **Linux host with KVM**, swap Docker sandboxes for CubeSandbox
**microVMs** (E2B HTTP protocol). microVMs give kernel-level isolation and
snapshot-based fast cold start, at the cost of requiring `/dev/kvm` and mTLS to
the CubeSandbox controller.

> Full procedure, including pre-flight checks, certificate generation, the
> compose override, smoke test, and rollback, is in the runbook:
> **[docs/runbook/enable-cubesandbox.md](./runbook/enable-cubesandbox.md).**

Summary:

| Step | Command |
|---|---|
| 1. Pre-flight (KVM, CPU flags, controller image, Docker) | `bash deploy/scripts/check-cube-preflight.sh` |
| 2. Generate mTLS certs (CA + controller + scheduler client) | `bash deploy/scripts/gen-cube-certs.sh` |
| 3. Export the controller image URI and (optional) cert dir | `export CUBE_CONTROLLER_IMAGE=...` |
| 4. Start with the cube override stacked on the base file | see below |

```bash
docker compose \
  -f deploy/docker-compose/agent-platform.yml \
  -f deploy/docker-compose/agent-platform.cube.yml \
  up -d --build
```

The override file (`agent-platform.cube.yml`) **stacks on top of** the base
compose file and:

- adds the `cube-controller` service (the microVM manager; image supplied via
  `CUBE_CONTROLLER_IMAGE`, sourced from the CubeSandbox project — no public image
  ships in this repo),
- sets `SCHEDULER_RUNTIME=cube` and mounts the mTLS client cert into the
  scheduler,
- sets `SANDBOX_POOL_RUNTIME=cube` pointing the pool at the same controller,
- leaves all other services unchanged.

Per-microVM egress is enforced by **CubeVS** (eBPF `allow_out` overlay,
`deploy/cube/sandbox-broker-only.policy.yaml`) with a host **nftables** fallback
(`deploy/cube/nftables/`). An empty allowlist fail-closes the spawn.

> **macOS / Docker Desktop cannot run the cube runtime** — no `/dev/kvm`. Docker
> is the dev default; CubeSandbox is the staging/production target.

To roll back to Docker sandboxes, restart **without** the override file; the base
file's `SCHEDULER_RUNTIME=docker` default takes over.

---

## Mode 3 — Kubernetes via Helm

The Helm chart lives at `deploy/helm/agent-platform`. It ships three profiles —
`small`, `medium`, `large` — selected with a `values-<profile>.yaml` file. The
chart body is unchanged across profiles; the values file drives replicas,
autoscaling, runtime, and security posture.

```bash
helm install agent-platform deploy/helm/agent-platform -f deploy/helm/agent-platform/values-small.yaml \
  --set global.ingress.host=agent-platform.your.domain \
  --set bff.config.cookieSecret=<>=32-chars-random>
```

> **Foundation subchart.** `foundation.enabled` defaults to **off** — the chart
> assumes a shared Foundation already runs in the cluster, and you point the
> services at it via `global.foundation.*` URLs and `existingSecret` references.
> Set `foundation.enabled=true` only if you want a fully self-contained install.
> The chart declares Foundation as a subchart dependency; resolve it with
> `helm dependency update` (a sibling `saas-foundation` checkout is needed at
> chart-build time).

### Profile comparison

| Aspect | `small` | `medium` | `large` |
|---|---|---|---|
| Target scale | POC / single-node K3s / ≤ 50 users | Enterprise private cloud / 3–10 nodes / 100–500 users | Cross-AZ / 1000+ users / SaaS master cluster |
| Replicas | 1 (no HA) | 2–3 + HPA | 3–10 + HPA (BFF/orchestrator 5) |
| Sandbox runtime | `docker` (docker-socket-proxy) | `docker` (can switch to `cube`) | `cube` (recommended) |
| Sandbox-pool scaling | fixed replica (KEDA off) | KEDA on `sandbox_pool_idle` | KEDA, higher watermarks |
| HPA / PDB | off | on (PDB `minAvailable: 1`) | on (PDB `minAvailable: 2`) |
| Topology spread | none | across nodes (`ScheduleAnyway`) | across zones (`DoNotSchedule`) |
| NetworkPolicy | off | on | on (strict) |
| Ingress | Traefik (K3s default), TLS off | nginx, TLS on | nginx, TLS on |
| Pod security context | default | default | hardened (`runAsNonRoot` 10001, drop ALL caps, `readOnlyRootFilesystem`, `RuntimeDefault` seccomp) |
| Workspace volume | `hostPath` | `hostPath` (RWX PVC recommended) | RWX PVC (CephFS/NFS), required for multi-replica scheduler |

```bash
# small  (K3s single node)
helm install agent-platform deploy/helm/agent-platform -f deploy/helm/agent-platform/values-small.yaml

# medium (HA private cloud)
helm install agent-platform deploy/helm/agent-platform \
  -f deploy/helm/agent-platform/values-medium.yaml \
  -f values-prod-secrets.yaml \
  --set global.ingress.host=agent-platform.your.domain

# large  (cross-AZ, cube runtime)
helm install agent-platform deploy/helm/agent-platform -f deploy/helm/agent-platform/values-large.yaml
```

### KEDA autoscaling for the sandbox pool

In `medium` and `large`, the sandbox pool is scaled by **KEDA** off the
`sandbox_pool_idle` Prometheus gauge the pool exports on `/metrics`. The
`ScaledObject` and trigger-auth manifests live in `deploy/operator/`. KEDA
requires a Prometheus that scrapes the pool. Tuning lives in the values file:

| Setting | `medium` | `large` |
|---|---|---|
| `sandboxPool.keda.enabled` | `true` | `true` |
| `sandboxPool.keda.minReplicaCount` | `1` | `2` |
| `sandboxPool.keda.maxReplicaCount` | `10` | `50` |
| `sandboxPool.keda.idleThreshold` | `5` | `10` |

> **Sandboxes are not Kubernetes Pods.** Whether the runtime is `docker`
> (sibling containers) or `cube` (microVMs), the agent sandbox is **not** a Pod,
> so a Pod-selecting `NetworkPolicy` would select nothing. The chart therefore
> **does not render** a sandbox NetworkPolicy by default
> (`templates/sandbox-isolation.yaml`). Sandbox isolation is enforced by (a) the
> broker egress URL-rewriting in the scheduler (in-repo) and (b) CubeSandbox
> controller-side per-VM policy (out-of-repo). Do not mistake the absent
> NetworkPolicy for missing isolation.

---

## Building the sandbox images

Agent runs execute inside an OCI image that runs the Claude Agent SDK worker
(`worker-claude-sdk/`, entrypoint `node /app/dist/main.js`, non-root `agent`
user uid 1000). It is the only agent runtime — see
[ADR 0003](adr/0003-claude-agent-sdk-sole-agent-runtime.md). **All build
contexts are the repo root** so the Dockerfiles can `COPY worker-claude-sdk/`.

| Image | Dockerfile | Notes |
|---|---|---|
| `ghcr.io/agent-platform/claude-sdk-sandbox:0.1` | `claude-sdk.Dockerfile` | Default image for the docker runtime. `node:22-slim` + prebuilt `worker-claude-sdk/dist` + Python office/data libs (`python-docx`, `openpyxl`, `python-pptx`, `pypdf`, `pandas`, `matplotlib`, …) and Noto CJK fonts. Pre-warmed by `sandbox-pool`. |
| `claude-sdk-cube:0.2-scientific` | `claude-sdk-cube.Dockerfile` | CubeSandbox-native image (Cube `sandbox-code` base, pinned digest) with scientific Python and CMake/native build tools; compiles the worker in a build stage. |
| `claude-sdk-cube:0.2-matlab-*` | `claude-sdk-cube-matlab.Dockerfile` + `build-matlab-*-layer.sh` | Optional MATLAB 2019b + Control System / Aerospace toolbox layers on top of the Cube image. **Bring your own installer media and license.** |
| *(refresh)* | `claude-sdk-cube-worker-refresh.Dockerfile` | Replaces only `/app/dist` + `/app/node_modules` in an existing Cube image, leaving heavyweight layers (MATLAB) untouched. |

The default image is a **two-step** build (compile TypeScript, then
containerise) — `claude-sdk.Dockerfile` copies a prebuilt `dist/`, it does not
run `tsc`:

```bash
make build-claude-sdk-image     # runs npm ci && npm run build, then docker build
make push-claude-sdk-image      # pushes to the configured registry
make build-claude-sdk-cube-image
```

**Selecting an image.** The image the scheduler launches is set per-deployment
via `AGENT_IMAGE` / `SCHEDULER_AGENT_IMAGE` (`agentScheduler.config.agentImage` /
`sandboxPool.config.sandboxImage` in Helm) and, for the cube runtime, per-run via
the Cube template (`CUBE_CLAUDE_SDK_TEMPLATE_SMALL` / `_LARGE`) in the pool
acquire payload.

> Bumping the Claude Agent SDK version is a coordinated change (update
> `worker-claude-sdk/package-lock.json`, rebuild + retag, run the worker tests and
> the conformance suite, then bump the scheduler default). Cosign signing + syft
> SBOM are on the roadmap. See [sandbox-images/README.md](../sandbox-images/README.md).

---

## Configuration and secrets

Agent Platform follows a single-source-of-truth secrets model: real
infrastructure credentials live on the broker and the foundation side; sandboxes
hold none. For local dev these come from `deploy/docker-compose/.env`
(see [`.env.example`](../deploy/docker-compose/.env.example)); in Kubernetes they
come from `existingSecret` references so the chart never bakes literals.

### Required secrets

| Secret | Purpose | Generate / source |
|---|---|---|
| `BFF_COOKIE_SECRET` | Signs the `ap_session` browser cookie. `CookieSigner` rejects empty/placeholder/`<32`-char values at startup. | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `BFF_SESSION_TOKEN_KEYS` | Comma-separated Fernet keys that encrypt the identity access/refresh tokens the BFF keeps per browser session (`web_sessions`). The first key encrypts, every key decrypts: rotate by putting a new key first, and drop the old one once the sessions it encrypted have ended (identity's absolute session timeout, 8 h by default). A session whose tokens no key can decrypt simply ends and the user signs in again. Empty = a key derived from `BFF_COOKIE_SECRET`, acceptable for dev only. | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `TOOL_BROKER_LEASE_SECRET` + `SCHEDULER_TOOL_BROKER_LEASE_SECRET` | Shared HMAC anchor for per-run sandbox capability leases. The scheduler signs; the broker verifies. **Must be identical.** A leaked/misconfigured value lets a sandbox self-sign leases and bypass tenant/scope boundaries. | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SCHEDULER_SERVICE_TOKEN` | Lets the scheduler mint short-lived per-run sandbox tokens from Foundation Identity. Scope limited to service-account token issuance. **Never expose to sandboxes** — only the per-run minted token belongs in a sandbox. | Issued by Foundation Identity. |
| `APP_USER_PASSWORD` / `APP_MIGRATOR_PASSWORD` | Postgres roles inherited from Foundation (least-privilege app user vs. migrator). | Operator-set; never reuse the `*_change_me` placeholders in production. |

> **No model provider key.** All agent model traffic goes through the Foundation
> LLM gateway: provider keys are configured on the gateway (saas-foundation; in the
> web app under Admin → Models), not here, and the scheduler gives each run a
> short-lived virtual key bound to the tenant's provider. `LLM_API_KEY`,
> `LLM_PROVIDER`, `LLM_BASE_URL` and `RUNTIME_EXTENSIONS` are no longer read. A
> tenant with no enabled provider cannot start runs: they are refused with
> `AGENT.RUN.LLM_NOT_CONFIGURED`, and the user is told an administrator has to add one.

> **Generation pattern.** All HMAC/cookie secrets use the same recipe:
> `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Each must be
> ≥ 32 characters and must not contain placeholders like `change-me` /
> `REPLACE_ME`; the relevant services validate and fail-closed at startup.

### Kubernetes secret wiring

In Helm, inject secrets via `existingSecret` rather than chart values:

| Chart field | Backing Secret |
|---|---|
| `bff.existingSecret` | `BFF_COOKIE_SECRET`, `BFF_SESSION_TOKEN_KEYS` |
| `toolBroker.existingSecret` | `TOOL_BROKER_LEASE_SECRET` (the scheduler **reuses the same Secret** — do not create a second one) |
| `agentScheduler.existingSecret` | scheduler service token, Keycloak client secret |
| `global.foundation.database.existingSecret` | Postgres app/migrator credentials |
| `global.foundation.redis.existingSecret` / `global.foundation.s3.existingSecret` | Redis / MinIO credentials (consumed by the tool-broker façades) |

### DLP failure policy

`DLP_FAILURE_MODE` (compose) / `global.foundation.dlpFailureMode` (helm) decides
what happens when a Foundation DLP check cannot be completed (DLP unreachable,
timed out, non-2xx or unparseable answer). `closed` (the default) refuses: the
sandbox denies the tool call, and the session-orchestrator answers an upload or
download with 503 `AGENT.DLP.UNAVAILABLE`. `open` lets it through and logs an
error every time. Both services log the effective policy at startup. Deploy the
Foundation DLP release that lets any tenant caller scan before (or together
with) new sandbox images: an older DLP refuses those scans, and with `closed`
every tool call would be denied. See [`dlp-real-defense-plan.md`](dlp-real-defense-plan.md).

### Egress mode

`sandboxEgressMode` controls how sandboxes reach infrastructure:

| Mode | Behaviour |
|---|---|
| `broker` (default) | Sandboxes reach infra **only** through the tool-broker façade, gated by a signed per-run HMAC lease. |
| `foundation` | Legacy/compat mode: sandboxes reach the Foundation service network directly. |
| `none` | Local-filesystem tools only. |

### Logging

Every service in `services/` (except the dev-only `echo-mcp` example) sets up
logging once at startup, in `create_app`, with OpenTelemetry on or off. It
writes one JSON object per line to stderr:
`timestamp` (UTC, ISO 8601), `level`, `service`, `logger`, `message`, then
`exception` (the formatted traceback) or `stack` when present, and any fields
the code passed as `extra=`. Each line is built with `json.dumps`, so quotes,
newlines and tracebacks cannot break it. uvicorn's startup and access lines,
Python warnings and SQL echo come out the same way.

| Variable | Service |
|---|---|
| `BFF_LOG_LEVEL` | bff |
| `SESSION_ORCHESTRATOR_LOG_LEVEL` | session-orchestrator |
| `SCHEDULER_LOG_LEVEL` | agent-scheduler |
| `SANDBOX_POOL_LOG_LEVEL` | sandbox-pool |
| `TOOL_BROKER_LOG_LEVEL` | tool-broker |
| `MCP_REGISTRY_LOG_LEVEL` | mcp-registry |
| `SKILL_STORE_LOG_LEVEL` | skill-store |
| `APP_REGISTRY_LOG_LEVEL` | app-registry |
| `KNOWLEDGE_INGEST_LOG_LEVEL` | knowledge-ingest |
| `CRON_RUNNER_LOG_LEVEL` | cron-runner |
| `WEB_FETCH_LOG_LEVEL` | web-fetch-gateway |

- Values are `DEBUG`, `INFO` (the default), `WARNING`, `ERROR` and `CRITICAL`,
  in any case. Any other value stops the service at startup.
- The level applies to the service's own code. Libraries (uvicorn, httpx,
  botocore, SQLAlchemy, ...) never log below INFO, because their DEBUG output
  carries request and response headers, request bodies and bound SQL
  parameters. A level above INFO quiets them too.
- uvicorn's access log and httpx's request log show URLs without their query
  string (`?[redacted]`), which can carry an OAuth `code` or a presigned S3
  signature.
- SQL echo is a separate, opt-in setting of each service with a database
  (`<PREFIX>SQL_ECHO`, default false). Bound parameters are hidden either way,
  in the echo and in database error messages.
- Compose defaults every service to INFO; set a variable in
  `deploy/docker-compose/.env` to change one. Use DEBUG only for targeted
  debugging: on the session-orchestrator's SSE path it can back up
  `docker logs` enough to stall the stream. The Helm chart sets none of these,
  so services run at INFO there.

---

## Backup, restore, and disaster recovery

Backup/restore scripts live in `deploy/backup/`. For the local docker-compose
stack they `pg_dump` the databases and `mc mirror` the MinIO buckets; production
equivalents (pgBackRest / Velero / clickhouse-backup) are marked in the scripts.

```bash
# Back up Postgres databases + MinIO buckets to a target dir
bash deploy/backup/backup.sh --target-dir /path/to/backup

# Dry run (print actions only)
bash deploy/backup/backup.sh --dry-run

# Restore from a backup
bash deploy/backup/restore.sh

# Verify a restore is consistent
bash deploy/backup/verify-restore.sh
```

What is covered: Foundation/Keycloak Postgres databases, the `agent-platform`
and `foundation-audit-worm` (WORM) MinIO buckets, and the Vault KEK. Targets are
**RTO < 30 min / RPO < 1 hour**. The full cross-repo backup/restore topology is
documented in the [backup-restore runbook](./runbook/backup-restore-b-m6.md).

---

## Load testing

A [Locust](https://locust.io/) load test in `deploy/loadtest/` simulates the
end-to-end browser journey — BFF login → create session → chat over SSE — and
measures time-to-first-token and total-response SLOs.

```bash
pip install -r deploy/loadtest/requirements.txt
locust -f deploy/loadtest/locustfile.py --host http://localhost:8005
```

A companion `deploy/loadtest/monitor_resources.sh` samples container resource
usage during the run. Sample HTML reports from prior runs are kept under
`deploy/loadtest/results/`.

---

## Related documentation

- [Getting started](./getting-started.md) — local walkthrough and ports reference.
- [Enable CubeSandbox runbook](./runbook/enable-cubesandbox.md) — full staging
  switch procedure.
- [Backup / restore runbook](./runbook/backup-restore-b-m6.md) — DR topology and
  targets.
- [KEDA operator README](../deploy/operator/README.md) — sandbox-pool autoscaling.
- [CubeSandbox egress README](../deploy/cube/README.md) — per-microVM egress
  isolation.
- [Sandbox images README](../sandbox-images/README.md) — image tiers and pinning.
- [SaaS Foundation][foundation] — the enterprise base this platform requires.

## License

Apache-2.0. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
