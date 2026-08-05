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

### Added

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
