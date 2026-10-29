# Getting started

This guide takes you from a fresh clone to a running Agent Platform on your
laptop in roughly **5–15 minutes**. By the end you will have the foundation
stack, the agent services, and the web UI running locally, and you will have
triggered your first agent run.

Agent Platform is built **on top of** [SaaS Foundation][foundation] and cannot
build, start, or run without it. Foundation provides identity, authorization,
billing, audit, and the LLM gateway; Agent Platform imports `foundation_sdk`
and calls Foundation services at runtime. You therefore need **both repositories
checked out side by side** before you begin.

[foundation]: https://github.com/chenzefeng09/saas-foundation

> **Heads-up on the dev sandbox runtime.** The default local runtime is
> **Docker** — agent sandboxes run as sibling Docker containers spawned through
> a least-privilege docker-socket-proxy. **No KVM is required.** The
> microVM-based CubeSandbox runtime is a staging/production target and is
> covered separately in the [deployment guide](./deployment.md).

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Docker + Docker Compose | Compose v2 (`docker compose`) | Runs the foundation stack, the agent services, and dev sandboxes. |
| GNU make + bash | bash 4+ | The top-level `Makefile` runs with `bash -eu -o pipefail`. |
| Python | 3.12+ with [Poetry](https://python-poetry.org/) | Per-service virtualenvs; used by `make bootstrap`. |
| Node.js | 20+ with npm | Frontend (Vite 6). Node 22 is needed only if you build the optional Claude SDK worker image. |
| `saas-foundation` checkout | sibling directory `../saas-foundation` | Provides the foundation dev stack and the `foundation_sdk` path dependency. |

The data-plane infrastructure — **PostgreSQL 16 + pgvector, Redis, MinIO/S3,
and Keycloak** — is provided by the foundation stack, not by this repository.
You do not install them separately.

> **Windows / macOS.** The top-level `Makefile` is bash + GNU-make oriented.
> On Windows, run it from WSL2. On macOS, Docker Desktop is fine for the Docker
> runtime (CubeSandbox is not available on macOS — see the deployment guide).

---

## Step 1 — Clone both repositories side by side

The two repos must be siblings. The build wiring assumes
`../saas-foundation` relative to the Agent Platform checkout.

```bash
# Pick a parent directory, then clone both into it:
git clone https://github.com/chenzefeng09/saas-foundation.git
git clone https://github.com/chenzefeng09/agent-platform.git

# Resulting layout:
#   <parent>/
#     ├── saas-foundation/
#     └── agent-platform/
```

All remaining commands are run from inside `agent-platform/` unless noted.

> **`FOUNDATION_DIR`.** The Makefile resolves the foundation checkout through a
> `FOUNDATION_DIR` variable. If your foundation checkout is **not** at
> `../saas-foundation`, override it on every make invocation, e.g.
> `make up FOUNDATION_DIR=/path/to/saas-foundation`.

---

## Step 2 — Create your `.env`

```bash
make env
```

This copies `deploy/docker-compose/.env.example` to
`deploy/docker-compose/.env` if one does not already exist. The `.env` file is
git-ignored. Open it and fill in the required secrets before starting the stack:

| Variable | Why it is required | How to generate |
|---|---|---|
| `BFF_COOKIE_SECRET` | Signs the browser session cookie. `foundation_sdk.CookieSigner` validates it at startup and **refuses to boot** on an empty or placeholder value (must be ≥ 32 chars, no `change-me`). | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `TOOL_BROKER_LEASE_SECRET` and `SCHEDULER_TOOL_BROKER_LEASE_SECRET` | Shared HMAC secret that signs per-run sandbox capability leases. The scheduler signs with it; the tool-broker verifies with it. **Both must be set to the same value.** | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `SCHEDULER_SERVICE_TOKEN` | Lets the scheduler mint short-lived per-run sandbox tokens from Foundation Identity. | Issued by the foundation identity service (scope limited to service-account token issuance). |

The remaining defaults (ports, pool watermarks, `FOUNDATION_NETWORK`,
`FOUNDATION_SDK_PATH`) work as-is for a sibling-checkout layout.

No model provider key goes in this file: agent runs reach models only through
the foundation LLM gateway with a per-tenant virtual key, so make sure your
tenant has one configured on the foundation side — a run for a tenant without a
virtual key fails at spawn.

---

## Step 3 — Bootstrap the service virtualenvs

```bash
make bootstrap
```

This runs `poetry install` for every service and then installs the foundation
SDK editable into each service venv (`foundation_sdk[fastapi]` from
`../saas-foundation/sdks/python`). The SDK is **not** on public PyPI, so this
step is required on a fresh checkout — it is what makes `import foundation_sdk`
resolve. If the foundation checkout is missing or in the wrong place, this step
fails fast with a clear error.

---

## Step 4 — Start the stack

```bash
make up
```

`make up` brings up the **foundation dev stack** (Postgres, Redis, MinIO,
Keycloak, the LLM gateway, identity/authz/billing/audit, …) and then the
**agent services** defined in `deploy/docker-compose/agent-platform.yml`. The
agent services attach to the external Docker network the foundation project
creates (`saas-foundation-dev_foundation` by default).

If the foundation stack is **already running** and you only want to (re)start
the agent services:

```bash
make agent-up
```

Build the sandbox image the scheduler launches for every run (the Claude Agent
SDK worker, `AGENT_IMAGE=ghcr.io/agent-platform/claude-sdk-sandbox:0.1`):

```bash
make build-claude-sdk-image
```

Useful companions while the stack runs:

```bash
make ps                 # show agent service status
make logs SVC=bff       # follow one service's logs
make down               # stop agent services
make nuke               # stop everything and destroy volumes
```

---

## Step 5 — Run database migrations

```bash
make migrate
```

This runs the session-orchestrator Alembic migrations
(`alembic upgrade head`), creating its schema in Postgres. (Other services
own their own Alembic envs and migrate on their own paths.)

---

## Step 6 — Start the web frontend

```bash
make web-install      # npm install (first time only)
make web-dev          # start the Vite dev server
```

Then open:

```
http://localhost:5173
```

The web app boots through the BFF: `AuthGate` calls `GET /api/bff/v1/auth/me`,
which verifies the signed `ap_session` cookie against Foundation Identity (via
JWKS) before routing you into the workspace.

---

## Ports reference

Default **host** ports for the local docker-compose dev stack. Where a service's
in-container port differs or collides with a foundation service, the dev compose
file remaps it to a distinct host port — those cases are called out.

### Agent Platform services

| Service | Host port | Notes |
|---|---|---|
| Web frontend (Vite dev server) | `5173` | The URL you open in the browser. |
| BFF (backend-for-frontend) | `8005` | Auth gateway, API aggregation, SSE relay. |
| Session orchestrator | `8006` | Session / message / run CRUD + run SSE. |
| Agent scheduler | `8007` | Spawns sandbox containers/microVMs. |
| Sandbox pool | `8008` | Pre-warms idle sandboxes; exposes `/metrics`. |
| MCP registry | `8010` | Per-tenant MCP server registry. |
| Skill store | `8011` | Versioned skill-package registry. |
| Knowledge ingest | `8012` | Document → embedding → pgvector pipeline. |
| App registry | `8014` (host) | Container port `8013`; remapped because foundation-audit uses `8013`. |
| Cron runner | `8015` (host) | Container port `8013`; runs published Apps on a schedule. |
| Web-fetch gateway | `8023` (host) | Container port `8013`; SSRF-protected outbound HTTP fetch. |
| Tool broker | `8024` | Broker-mode sandbox egress façade. |

> **Port collisions, explained.** App-registry, cron-runner, and web-fetch-gateway
> all listen on container port `8013`. Foundation-audit also uses `8013`. The dev
> compose file resolves this by mapping each to a distinct **host** port
> (`8014`, `8015`, `8023`). Inside the Docker network the services talk to each
> other by service name, so the container-port overlap is harmless.

### Foundation services (provided by the sibling stack)

These come up with the foundation stack and the agent services depend on them.
Host ports follow the foundation dev compose mapping.

| Service | Host port |
|---|---|
| Identity | `8001` |
| Authz | `8002` |
| Org | `8003` |
| Crypto | `8004` |
| LLM gateway | `8011` (container) |
| Feature flag | `8012` (container) |
| Audit | `8013` |

> Several foundation services are remapped to high host ports (e.g. billing
> `8017`, data-protection `18016`) in the dev compose file to avoid colliding
> with Agent Platform's `8005`–`8014` range. Consult the foundation repo's
> compose file for the authoritative host-port map.

---

## Trigger your first agent run

Once the stack is up and you are signed in at `http://localhost:5173`:

1. **From the UI** — open a session (the `/tasks/:id` AgentPage), type a prompt,
   and send it. The BFF relays your message to the session-orchestrator, which
   starts a run via the scheduler; the scheduler spawns a Docker sandbox running
   the Claude Agent SDK worker. Token deltas and plan/step state stream back to the
   browser over SSE until the run reaches `done`.

2. **From the API** — drive the scheduler directly with a valid session bearer
   token:

   ```bash
   curl -f -X POST http://localhost:8007/api/scheduler/v1/runs \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{
       "session_id": "ses_test001",
       "task": "echo hello from the sandbox"
     }'
   ```

   Watch it execute:

   ```bash
   make logs SVC=agent-scheduler
   ```

What happens under the hood: the scheduler mints a short-lived per-run
Foundation JWT, signs an HMAC tool-broker lease, and starts a sandbox through
the docker-socket-proxy. In the default **broker** egress mode the sandbox joins
only the internal `sandbox_broker` network and can reach infrastructure *only*
through the tool-broker façade; the web-fetch-gateway is the single service with
outbound internet access. The worker runs the task, writes
`/workspace/result.json`, streams events to Redis, and exits with a coded
status.

---

## Troubleshooting

### Foundation is not running

**Symptom:** agent services crash-loop or `make up` reports the external network
is missing; BFF login fails with connection errors to identity.

**Fix:** Agent Platform requires the foundation stack to be up first. `make up`
starts it for you, but if you started agent services on their own (`make agent-up`)
the foundation stack must already be running. Start it explicitly:

```bash
# from the saas-foundation repo:
cd ../saas-foundation/deploy/docker-compose && docker compose -f dev.yml up -d
```

### `ModuleNotFoundError: No module named 'foundation_sdk'`

**Symptom:** a Python service fails to import `foundation_sdk` on startup or in
tests.

**Fix:** the SDK is a path dependency from the sibling foundation repo, not a
PyPI package. Re-run the SDK install:

```bash
make install-sdk     # or: make bootstrap
```

If it still fails, confirm `../saas-foundation/sdks/python` exists and that
`FOUNDATION_DIR` points at your foundation checkout.

### Docker network name mismatch

**Symptom:** `network saas-foundation-dev_foundation not found`, or agent
services cannot resolve foundation service names.

**Fix:** the agent services attach to the **external** network created by the
foundation compose project. The default is `saas-foundation-dev_foundation`. If
you renamed the foundation project, update `FOUNDATION_NETWORK` in
`deploy/docker-compose/.env` to match the actual network name (check with
`docker network ls`).

### Empty or weak cookie secret

**Symptom:** the BFF refuses to start, logging that `BFF_COOKIE_SECRET` is empty
or contains a placeholder.

**Fix:** this is intentional — `foundation_sdk.CookieSigner` validates the secret
at startup and fails fast rather than silently signing forgeable cookies. Set a
strong value in `deploy/docker-compose/.env` (≥ 32 chars, no `change-me`):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

The same applies to `TOOL_BROKER_LEASE_SECRET` /
`SCHEDULER_TOOL_BROKER_LEASE_SECRET` outside dev — set both to the same strong
value.

---

## Next steps

- **Deploy to staging or production** — see the
  [deployment guide](./deployment.md) for the Docker, CubeSandbox microVM, and
  Kubernetes (Helm) modes.
- **Switch to CubeSandbox microVMs** — see
  [docs/runbook/enable-cubesandbox.md](./runbook/enable-cubesandbox.md).
- **Understand the design** — the architecture decision records under
  [docs/adr/](./adr/) cover the sandbox runtime and agent-kernel choices.
- **SaaS Foundation** — the enterprise base this platform builds on lives at
  [github.com/chenzefeng09/saas-foundation][foundation].

## License

Apache-2.0. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE).
