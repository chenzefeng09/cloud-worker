# Runbook: Enable CubeSandbox on Staging

This document covers the complete procedure for switching agent-platform from
the default Docker-backed sandbox runtime to CubeSandbox microVMs on a Linux
staging host that has KVM hardware virtualisation.

**Why two runtimes?** — See ADR 0001 and `docs/design/b-m2-sandbox-runtime-v1.md`.
The short version: Docker containers work on any host (including macOS + Docker Desktop)
and are the dev/CI default; CubeSandbox microVMs require KVM and are the production
target because they provide stronger isolation and snapshot-based fast cold-start.

---

## Prerequisites

| Requirement | How to verify |
|---|---|
| Linux host (staging) | `uname -s` → `Linux` |
| KVM enabled | `/dev/kvm` exists and is readable |
| CPU virtualisation | `grep -E 'vmx\|svm' /proc/cpuinfo` returns output |
| Docker + compose plugin | `docker compose version` succeeds |
| `openssl` ≥ 1.1.1 | `openssl version` |
| CubeSandbox controller image URI | Provided by Tencent Cloud / CubeSandbox release |

The foundation stack must already be running:

```bash
# From the saas-foundation repo:
cd ../saas-foundation/deploy/docker-compose && docker compose -f dev.yml up -d
```

---

## Step 1 — Run the pre-flight check

```bash
bash deploy/scripts/check-cube-preflight.sh
```

This script checks `/dev/kvm`, CPU flags, the `CUBE_CONTROLLER_IMAGE` env var,
and confirms Docker is reachable.  Fix any `[FAIL]` items before continuing.

---

## Step 2 — Generate mTLS certificates

The CubeSandbox controller and the scheduler communicate over mTLS.
The script generates a self-signed CA, a controller server cert, and a
scheduler client cert.

```bash
# Default output: deploy/certs/
bash deploy/scripts/gen-cube-certs.sh
```

To verify the output:

```bash
openssl verify -CAfile deploy/certs/ca.crt deploy/certs/controller.crt
openssl verify -CAfile deploy/certs/ca.crt deploy/certs/scheduler-client.crt
```

The generated files are git-ignored (`deploy/certs/`).  Store them in your
secrets manager or a secure volume for staging; rotate before the expiry date
printed by the script (~825 days).

For production, replace the self-signed certs with certificates issued by
your internal PKI and skip this step.

---

## Step 3 — Set required environment variables

Export the following before starting the compose stack:

```bash
# REQUIRED: the CubeSandbox controller OCI image URI.
# Get this from Tencent Cloud / the CubeSandbox project release page.
export CUBE_CONTROLLER_IMAGE=ccr.ccs.tencentyun.com/cube/cube-controller:<TAG>

# Optional: if the certs are not in deploy/certs/, set the path.
export CUBE_CERTS_DIR=/path/to/certs

# Optional: override TLS mode (default: mtls)
# Use "tls" if an Istio/Linkerd sidecar handles client auth out-of-band.
# Use "off" only in air-gapped test environments.
# export CUBE_TLS_MODE=mtls

# Optional: override controller URL if your network differs.
# export CUBE_CONTROLLER_URL=https://cube-controller:8443
```

All remaining variables (`BFF_COOKIE_SECRET`, `AGENT_IMAGE`, etc.) are the
same as for the standard docker-backed stack — see `.env.example`.

---

## Step 4 — Start the stack with the cube override

The cube override file (`agent-platform.cube.yml`) **stacks on top of** the
base compose file.  Docker Compose merges environment and volume keys:

```bash
docker compose \
  -f deploy/docker-compose/agent-platform.yml \
  -f deploy/docker-compose/agent-platform.cube.yml \
  up -d --build
```

This brings up:
- `cube-controller` — the CubeSandbox microVM manager (placeholder image, see Step 3)
- `agent-scheduler` — with `SCHEDULER_RUNTIME=cube` and the mTLS client cert mounted
- `sandbox-pool` — with `SANDBOX_POOL_RUNTIME=cube` pointing at the same controller
- All other services unchanged (bff, session-orchestrator, web, etc.)

---

## Step 5 — Verify the cube-controller is reachable

Once the stack is up, confirm mTLS connectivity from the scheduler's perspective:

```bash
# From the staging host (outside Docker):
curl -f \
  --cacert deploy/certs/ca.crt \
  --cert deploy/certs/scheduler-client.crt \
  --key deploy/certs/scheduler-client.key \
  https://localhost:8443/healthz

# Or from inside the agent-scheduler container:
docker exec agent-scheduler \
  python -c "
import httpx
c = httpx.Client(
    verify='/etc/cube/certs/ca.crt',
    cert=('/etc/cube/certs/scheduler-client.crt', '/etc/cube/certs/scheduler-client.key')
)
r = c.get('https://cube-controller:8443/healthz')
print(r.status_code, r.text)
"
```

---

## Step 6 — Smoke-test sandbox spawn

Run a test via the scheduler API to confirm end-to-end spawning:

```bash
# Replace <TOKEN> with a valid session bearer token.
curl -f -X POST http://localhost:8007/api/scheduler/v1/runs \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "ses_test001",
    "task": "echo hello from cubesandbox"
  }'
```

Watch the logs:

```bash
docker compose \
  -f deploy/docker-compose/agent-platform.yml \
  -f deploy/docker-compose/agent-platform.cube.yml \
  logs -f agent-scheduler cube-controller
```

Expected log line from the scheduler: `cube sandbox spawned` with a `sandbox_id`.

---

## Step 7 — Verify the pool is pre-warming

```bash
curl http://localhost:8008/api/sandbox-pool/v1/healthz | python -m json.tool
```

Confirm `idle` counts are rising (it may take 30 s for the first warm-up tick).

---

## Rollback

To revert to Docker-backed sandboxes, restart without the cube override file:

```bash
docker compose -f deploy/docker-compose/agent-platform.yml up -d
```

The scheduler will see `SCHEDULER_RUNTIME=docker` (the base file's default) and
stop calling the cube-controller.

---

## Configuration reference

### agent-scheduler cube settings

| Env var | Default (cube override) | Description |
|---|---|---|
| `SCHEDULER_RUNTIME` | `cube` | Set to `cube` to enable CubeSandbox |
| `SCHEDULER_CUBE_CONTROLLER_URL` | `https://cube-controller:8443` | Controller base URL |
| `SCHEDULER_CUBE_TLS_MODE` | `mtls` | `mtls` / `tls` / `off` |
| `SCHEDULER_CUBE_CLIENT_CERT` | `/etc/cube/certs/scheduler-client.crt` | Path inside container |
| `SCHEDULER_CUBE_CLIENT_KEY` | `/etc/cube/certs/scheduler-client.key` | Path inside container |
| `SCHEDULER_CUBE_CA_CERT` | `/etc/cube/certs/ca.crt` | Path inside container |

### sandbox-pool cube settings

| Env var | Default (cube override) | Description |
|---|---|---|
| `SANDBOX_POOL_RUNTIME` | `cube` | Set to `cube` to use PoolCubeRuntime |
| `SANDBOX_POOL_CUBE_CONTROLLER_URL` | `https://cube-controller:8443` | Controller base URL |
| `SANDBOX_POOL_CUBE_TLS_MODE` | `mtls` | Mirrors scheduler setting |
| `SANDBOX_POOL_CUBE_CLIENT_CERT` | `/etc/cube/certs/scheduler-client.crt` | Path inside container |
| `SANDBOX_POOL_CUBE_CLIENT_KEY` | `/etc/cube/certs/scheduler-client.key` | Path inside container |
| `SANDBOX_POOL_CUBE_CA_CERT` | `/etc/cube/certs/ca.crt` | Path inside container |

---

## Known limitations and follow-ups

### Snapshot-based pre-warming (B-M2 follow-up)

`PoolCubeRuntime.start_idle_container()` currently issues a regular cold spawn
(`POST /sandboxes`) rather than forking from a memory checkpoint.  The pool
still provides a latency benefit (the microVM is already booted at acquire time)
but not the sub-100 ms target that snapshot forking would achieve.

Implementing snapshot pre-warming requires:
1. Spawning a "template VM" and waiting for the worker to reach a quiescent state.
2. Calling the CubeSandbox snapshot API (not yet publicly documented in the E2B spec).
3. Forking new VMs from that snapshot on every `start_idle_container` call.

This is tracked as a follow-up once the snapshot API surface is confirmed.

### CubeSandbox controller image

The `cube-controller` service in `agent-platform.cube.yml` uses a placeholder
image (`${CUBE_CONTROLLER_IMAGE:?...}`).  The real image must be sourced from
Tencent Cloud / the CubeSandbox project.  The placeholder will cause a startup
failure (compose validates the variable at parse time) if `CUBE_CONTROLLER_IMAGE`
is not set — this is intentional to prevent silent misconfiguration.

### Local macOS / Docker Desktop

macOS + Docker Desktop does not expose `/dev/kvm` (no nested virtualisation).
The cube runtime **cannot run locally**.  This is documented in ADR 0001 and
by design: Docker is the dev default; cube is the staging/production target.
The staging-ready materials in this repo (compose override, certs, this runbook)
are everything needed to switch on a suitable Linux host.
