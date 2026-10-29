# B-M2 — Sandbox runtime + AgentScope worker (v1)

> **Historical note (2026-10).** This design was written for the AgentScope
> worker (`agentscope-ext/`), which has since been removed. The sandbox-runtime
> contracts (`SandboxRuntime` protocol, run state machine, pool, egress) still
> hold; the agent side is now the Claude Agent SDK worker — see
> [ADR 0003](../adr/0003-claude-agent-sdk-sole-agent-runtime.md) and
> [contracts/worker-wire-contract.md](../../contracts/worker-wire-contract.md).

> **Phase**: B-M2  ·  **Status**: skeleton landed, real wiring is B-M2 work
>
> See also:
> - [ADR 0001 — Sandbox runtime](../adr/0001-sandbox-runtime-cube-default-kata-fallback.md)
> - [ADR 0002 — Agent kernel: AgentScope](../adr/0002-agent-kernel-agentscope-over-openhands.md)
> - top-plan §B.1 (仓库结构) + §B.2 (B-M2 里程碑)

## 1. Context — why this milestone exists

B-M1 hard-coded Docker container spawning into ``agent-scheduler`` and
asked OpenHands to do the agent work inside.  Two things needed to
change for B-M2:

* **Kernel isolation.**  Docker shares the host kernel — wrong primitive
  for executing untrusted model-generated code.  We swap in
  **CubeSandbox** (microVM, E2B HTTP protocol) per ADR 0001.
* **Agent framework fit.**  OpenHands is a platform; we already own the
  platform layer (BFF, orchestrator, sandbox-pool, foundation).  We
  swap in **AgentScope 2.x** (framework) per ADR 0002.

Both changes go through one new abstraction — the ``SandboxRuntime``
protocol — and one new contract — the ``agentscope_ext.runtime_worker``
entrypoint.  This document pins both.

## 2. Public contract

### 2.1 ``SandboxRuntime`` protocol

```python
# services/agent-scheduler/.../services/runtime.py
class SandboxRuntime(Protocol):
    def spawn_run(
        self, run_id: str, session_id: str, task: str,
        *, tenant_id: str, user_id: str, foundation_token: str,
    ) -> str: ...
    def get_run_status(self, container_id: str) -> dict: ...
    def get_container_tenant(self, container_id: str) -> str: ...
    def cancel_run(self, container_id: str) -> None: ...
    def list_containers(self, *, tenant_id: str) -> list[dict]: ...
```

Three implementations register themselves at import:

| Kind | Module | Use |
|---|---|---|
| ``docker`` | ``docker_runtime.DockerRuntime`` | Dev / CI (no KVM required) |
| ``cube``   | ``cube_runtime.CubeSandboxRuntime`` | Production default |
| ``kata``   | (reserved) | Fallback per ADR 0001 |

Selected by ``SCHEDULER_RUNTIME=docker|cube|kata`` env var; resolved at
app startup by ``build_runtime`` and cached on ``app.state.runtime``.

### 2.2 Sandbox ↔ worker wire contract

What the runtime puts into the sandbox:

| Channel | Carrier | Content |
|---|---|---|
| Env vars | ``FOUNDATION_API_URL`` / ``FOUNDATION_API_TOKEN`` (short-lived JWT) / ``FOUNDATION_TENANT_ID`` / ``FOUNDATION_USER_ID`` / ``LLM_MODEL`` / ``LLM_API_KEY`` / ``OTEL_EXPORTER_OTLP_ENDPOINT`` | identity + LLM + observability |
| Task | ``AGENT_TASK_JSON`` env var (cube) **or** ``/workspace/task.json`` file (docker) | ``{"task": str, "mcp_servers": [...], "skills": [...]}`` |
| Workspace | ``/workspace`` bind-mount (docker) / volume (cube) | writable scratch space + result.json drop point |

What the worker writes back:

| Path | Shape | Read by |
|---|---|---|
| ``/workspace/result.json`` | ``{"status": "OK"|"FAILED", "summary": str, "error": str?}`` | runtime polling |
| ``/workspace/events.jsonl`` | one JSON event per line (MsgHub stream) | future agent-platform audit forwarder |
| (exit code) | 0 = ok, 1 = infra, 2 = bad task, 137 = OOM | runtime polling |

The contract intentionally **decouples** the worker (AgentScope) from
the scheduler (Python service) so neither leaks into the other's
release cycle.  Swapping to a different agent framework one day is a
question of writing a new ``runtime_worker`` that honours this
contract.

### 2.3 OpenAPI contracts touched

* ``contracts/openapi/agent-scheduler.yaml`` — request/response shapes for
  ``POST /runs`` ``GET /runs/{id}`` ``POST /runs/{id}/cancel``
  ``GET /sandboxes`` ``POST /sandboxes/{id}/stop``.  No changes from
  B-M1 (the wire shape is runtime-agnostic).
* ``contracts/openapi/sandbox-pool.yaml`` — new in B-M2.  ``acquire`` /
  ``release`` / ``_pool``.

## 3. Data model

There's no new persistent table — runs / sessions are still in
session-orchestrator (B-M1).  The sandbox-pool keeps **in-memory** state
(``Pool.entries`` dict) in the skeleton; B-M2 follow-up replaces that
with a KEDA operator that reads pool state from the CubeSandbox
controller itself, so a pod restart doesn't drop warm VMs.

## 4. State machine (per run)

```
   POST /runs
       │
       ▼
   create_run (orchestrator) ─── mint foundation_token (identity)
       │                              │
       │ X-Tenant-Id passed through   │
       ▼                              ▼
   scheduler.spawn_run ──── runtime.spawn_run
       │                              │
       │                              ▼ (env + task delivered)
       │                       sandbox boots
       │                              │
       │                              ▼
       │                  runtime_worker reads env+task,
       │                  builds ReActAgent(toolkit, mcp_servers),
       │                  runs task to completion,
       │                  writes /workspace/result.json,
       │                  exits with code
       │                              │
       ▼                              ▼
   runtime.get_run_status polls until terminal state
       │
       ▼
   orchestrator marks run COMPLETED / FAILED / CANCELLED
```

## 5. Pool watermark logic

Configured in ``sandbox-pool/config.py``; three knobs:

```
idle_low_watermark  — drop below this → background warm-up spawns more
idle_high_watermark — rise above this → reap excess idle VMs
max_total           — hard cap; acquire returns 429 if reached
```

Skeleton implementation in ``Pool`` is single-process, single-template,
in-memory.  B-M2 follow-ups:

* Replace ``_spawn_one`` / ``_reap_one`` with HTTP calls to CubeSandbox.
* Snapshot-based warm pool (don't actually boot fresh, restore from
  snapshot — the latency win CubeSandbox advertises).
* KEDA operator for cross-pod state.
* Per-template warm-up (``base`` always, ``office`` / ``matlab`` when a
  tenant on those tiers exists).

## 6. Deployment forms

| Profile | runtime | pool size | acquire latency goal |
|---|---|---|---|
| **small** (POC, ≤50 users) | ``docker`` | ``idle_low=0`` (cold spawn each time) | < 2 s |
| **medium** (100–500 users) | ``cube`` | ``idle_low=5, max_total=50`` | < 200 ms warm |
| **large** (SaaS) | ``cube`` | ``idle_low=50, max_total=500`` | < 200 ms warm |

All three are the same code; values come from helm ``values.profile``.

**Sandbox egress isolation (cube runtime).**  In broker egress mode the
scheduler stamps ``agent-platform.network-policy=sandbox-broker-only`` on each
microVM; the concrete per-VM egress policy that physically confines it to the
tool-broker façade + DNS (default-deny everything else — the prod analogue of
the dev ``internal: true`` ``sandbox_broker`` network) ships as a deploy asset
in ``deploy/cube/`` (CubeVS ``allow_out`` overlay + host nftables fallback).

## 7. Compliance mapping

| Control | How B-M2 delivers it |
|---|---|
| ISO/IEC 27001:2022 A.8.15 Logging / SOC 2 CC7.2 (system monitoring) | the worker writes events.jsonl per run; run events are persisted platform-side for transcript and session replay |
| ISO/IEC 27001:2022 A.8.22 Segregation of networks · A.8.31 Separation of environments / SOC 2 CC6.6 (boundary protection) | microVM (CubeSandbox) — kernel isolation per ADR 0001; default-deny egress via tool-broker |
| ISO/IEC 27001:2022 A.8.32 Change management · A.8.28 Secure coding (supply chain) / SOC 2 CC8.1 | sandbox images built from pinned bases + lockfiles; cosign signing + syft SBOM are follow-ups |
| GDPR Art. 5(1)(c) data minimisation · Art. 25 data protection by design | short-lived foundation_token + per-run workspace; nothing persists across runs |

## 8. Open issues + follow-ups

* **No real AgentScope wiring yet.**  ``runtime_worker._run_skeleton``
  echoes the task; B-M2 swap to ``_run_real`` that builds
  ``ReActAgent(model=foundation_model, toolkit=tenant_toolkit)``.
* **CubeSandbox controller mTLS.**  ``CubeSandboxRuntime`` currently
  uses bare httpx; production needs client cert per ADR 0001.
* **Kata fallback cutover procedure.**  Documented at high level in
  ADR 0001; the actual runbook (drain pool, swap RuntimeClass, redeploy)
  belongs in ops docs once production hits.
* **Cold-vs-warm acquire telemetry.**  Skeleton returns
  ``cold_spawned: false`` always; track real cold/warm ratio so we can
  tune ``idle_low_watermark`` per tier with data.
* **events.jsonl forwarding.**  B-M3 ``event_audit_sink`` extension.
* **MCP injection.**  B-M3 ``mcp_tenant_injector`` extension.
* **Skills loading.**  B-M3 ``skills_loader`` extension.

## 9. Validation criteria (B-M2 sign-off)

| Test | How |
|---|---|
| ``SCHEDULER_RUNTIME=docker`` end-to-end echo task | curl agent-scheduler /runs → result OK |
| ``SCHEDULER_RUNTIME=cube`` against staging CubeSandbox | same curl, same result |
| Tenant isolation across runtimes | conformance suite tests cross-tenant get_run, list_sandboxes |
| Pool watermark holds under 100 concurrent runs | load-test acquire loop, check idle_low maintained |
| Worker exit code → orchestrator status mapping correct | unit test + sandbox echo run |
| Token revoked at run end / TTL expiry | identity audit shows no late calls on `foundation_token` |
