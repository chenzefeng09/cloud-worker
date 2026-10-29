# Changelog

All notable changes to Agent Platform are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the project is pre-1.0, minor (`0.x`) releases may include breaking changes; these
are always noted here.

Agent Platform is built on top of
[SaaS Foundation](https://github.com/chenzefeng09/saas-foundation) and requires a sibling
checkout of it (see [`CONTRIBUTING.md`](./CONTRIBUTING.md)).

## [Unreleased]

### Breaking changes

- **Claude Agent SDK is the only agent runtime** ([ADR 0003](./docs/adr/0003-claude-agent-sdk-sole-agent-runtime.md),
  superseding ADR 0002). The AgentScope 2.x Python worker and its extensions
  (`agentscope-ext/`: `runtime_worker`, `canvas_tool`, `event_audit_sink`, `knowledge_tool`,
  `llm_gateway_enforcer`, `mcp_tenant_injector`, `skills_loader`, `web_fetch`) are removed,
  as are the `base` / `bootstrap` / `overlay` / `office` / `matlab` sandbox Dockerfiles and
  `sandbox-images/wheels/`. Every run executes `node /app/dist/main.js` from
  `ghcr.io/agent-platform/claude-sdk-sandbox:0.1` (`sandbox-images/claude-sdk.Dockerfile`)
  or a `claude-sdk-cube*` image; `sandbox-pool` pre-warms the Claude SDK image.
- **Inline chat mode removed.** The session-orchestrator's in-process tool loop
  (`execution_mode='inline'`, with inline MCP / skills / knowledge / canvas tools,
  attachment text extraction and the optional `doc-to-md` converter) is gone; every run is
  sandboxed. `execution_mode` is still returned on runs for legacy rows, an incoming
  `inline` is coerced to `sandbox`, and the BFF no longer forwards the field. The
  `doc-to-md` service is removed from docker-compose.
- **Configuration keys removed:** `LLM_API_KEY`, `LLM_PROVIDER`, `LLM_BASE_URL`,
  `RUNTIME_EXTENSIONS`, `SCHEDULER_CLAUDE_SDK_IMAGE`, `USE_CLAUDE_SDK_WORKER_DEFAULT`, and the
  scheduler's per-run `enable_claude_sdk_worker` routing flag. `AGENT_IMAGE` /
  `SCHEDULER_AGENT_IMAGE` now point at the Claude SDK image.
- **LLM gateway is mandatory.** All model traffic goes through the foundation LLM gateway
  with a per-tenant virtual key (the worker's adapter translates Anthropic Messages to the
  gateway's OpenAI-compatible API). A missing virtual key now always fails the spawn.
- **Sandbox token scopes** no longer include `audit.write`.
- **Compliance targets international regimes only.** GDPR remains (ROPA, DPIA, DSAR,
  GDPR Art. 6 lawful bases). China-specific PIPL lawful bases / reports, the national
  cryptography feature flags (GMTLS, SM2/SM3/SM4) and MLPS references are removed.
- **English is the default UI and agent-facing language.** Chinese remains available as
  an optional UI locale.

### Changed

- Tenant knowledge-base top-k hits are prefetched by the orchestrator and appended to the
  worker's system prompt.

### Known gaps

- The Claude SDK worker has no canvas tool (the agent cannot create canvases), no
  on-demand knowledge search tool, and no in-sandbox audit sink. See ADR 0003.

### Added

- `docs/adr/0003-claude-agent-sdk-sole-agent-runtime.md`.
- Community and project files: `CONTRIBUTING.md`, `SECURITY.md`, `GOVERNANCE.md`, and this
  changelog.

## [0.1.0] - 2026-06-15

First public release line: an isolated, multi-tenant runtime for AI agents.

### Added

- **Isolated agent runtime.** Each agent run executes one task in its own per-run sandbox,
  which serves as the tenant isolation, audit, and billing boundary. Three sandbox runtimes
  sit behind a common `SandboxRuntime` protocol and the same `task.json` → `result.json`
  worker contract, selected via `SCHEDULER_RUNTIME`:
  - **CubeSandbox** microVMs (default) over the E2B HTTP protocol;
  - **Kata Containers** microVMs as the fallback;
  - **Docker** sibling containers (via a least-privilege docker-socket-proxy) for local
    development and CI.
- **Eleven backend services**, each an independent Python 3.12 / FastAPI / Poetry project:
  BFF (auth gateway + API aggregation + SSE relay), session-orchestrator (session/message/
  run lifecycle + attachments), agent-scheduler (sandbox spawn + per-run lease minting),
  sandbox-pool (warm-sandbox pre-warming with a Prometheus gauge), app-registry
  (templatized agent tasks with a review/publish workflow), skill-store (versioned skill
  packages), mcp-registry (per-tenant MCP servers), tool-broker (policy-controlled egress
  façade), knowledge-ingest (document → pgvector ingest and semantic search), cron-runner
  (scheduled App execution), and web-fetch-gateway (SSRF-protected outbound fetch).
- **React console.** A React 19 + TypeScript + Vite operator/user console with live SSE
  chat streaming, templatized App run/result/editor views, schedules, file and library
  management, in-browser PDF/DOCX/XLSX previews, an admin/governance shell gated by
  per-module permissions, and English/Chinese localization.
- **Agent kernel and sandbox extensions.** An AgentScope-based runtime worker plus
  standalone extension packages (`runtime_worker`, `llm_gateway_enforcer`,
  `mcp_tenant_injector`, `skills_loader`, `event_audit_sink`, `web_fetch`) installed into
  the sandbox image, with a base/office/matlab image tier lineage and a separate Node-based
  Claude Agent SDK worker honoring the same run contract.
- **Foundation integration.** Services consume `foundation_sdk` and call SaaS Foundation
  for identity, authorization, billing/quota, audit, and the LLM gateway; per-run
  short-lived, scope-limited tokens and tenant LLM virtual keys are used throughout.

### Security

- **MicroVM isolation by default** for untrusted, model-generated code; host-kernel-sharing
  Docker is restricted to development and CI.
- **Default-deny broker egress** — in broker mode a sandbox can reach only the tool-broker
  façade plus DNS; egress is enforced at the microVM layer with a host-firewall fallback,
  and an empty allow-list fails closed.
- **Per-run capability leases** — every tool-broker call (LLM, MCP, skills, web-fetch,
  state, object storage) must carry a signed, short-lived HMAC lease bound to
  tenant/user/run/scope/expiry. Real infrastructure credentials live only on the broker.
- **Hardened secret delivery** — control-plane secrets are delivered via a mode-0600 file
  the worker loads into userspace and immediately unlinks, keeping them out of the
  container environment.
- **LLM-gateway enforcement** — all sandbox LLM traffic is routed through the Foundation
  LLM gateway with a per-tenant virtual key; missing keys fail the run rather than falling
  back to direct provider credentials.
- **Data Loss Prevention (DLP)** — a default rule library scans sensitive data on upload,
  download, run-artifact persistence, and output/transcript paths, with results recorded
  for the governance console.
- **Tool-broker and web-egress hardening** — the broker strips client-injected identity
  headers, requires a valid lease, enforces an upstream path allow-list, disables OpenAPI
  docs, and strips its `Server` header; the web-fetch gateway is the sole internet-egress
  service and enforces domain allow-listing, private-range/metadata-IP blocking,
  content-type restrictions, and size caps.

### Deployment

- **Docker Compose** for local development (with a CubeSandbox override), a **Helm chart**
  with small/medium/large profiles and an optional Foundation subchart, a **KEDA**
  autoscaling operator driven by the sandbox-pool warm-VM gauge, CubeSandbox/host-firewall
  egress policy assets, and backup/restore and load-test tooling.

[Unreleased]: https://github.com/chenzefeng09/agent-platform/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/chenzefeng09/agent-platform/releases/tag/v0.1.0
