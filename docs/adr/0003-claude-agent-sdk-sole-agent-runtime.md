# ADR 0003 — Claude Agent SDK as the sole agent runtime

- **Status**: Accepted
- **Date**: 2026-10
- **Phase**: post-B-M6 (runtime consolidation)
- **Supersedes**: [ADR 0002 — Agent kernel: AgentScope, not OpenHands](0002-agent-kernel-agentscope-over-openhands.md)
- **Related**: [ADR 0001 — CubeSandbox default, Kata fallback](0001-sandbox-runtime-cube-default-kata-fallback.md) (unchanged), [contracts/worker-wire-contract.md](../../contracts/worker-wire-contract.md)

## Context

ADR 0002 chose AgentScope 2.x as the agent kernel and shipped it as a Python
worker (`agentscope-ext/`, `python -m agentscope_ext.runtime_worker`) plus
extension packages (`canvas_tool`, `event_audit_sink`, `knowledge_tool`,
`llm_gateway_enforcer`, `mcp_tenant_injector`, `skills_loader`, `web_fetch`).

Since then the platform grew a second worker, `worker-claude-sdk/` — a
TypeScript worker built on the
[Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) — that
honours the same sandbox runtime contract (`task.json` in, `result.json` +
`events.jsonl` out, same exit codes). The scheduler routed between the two
per run (`enable_claude_sdk_worker`, `SCHEDULER_CLAUDE_SDK_IMAGE`,
`USE_CLAUDE_SDK_WORKER_DEFAULT`), and the session-orchestrator additionally
kept a third execution path: an **inline** mode (`execution_mode='inline'`)
that ran an in-process OpenAI-style tool loop with its own MCP / skills /
knowledge / canvas tools, attachment text extraction and an optional
`doc-to-md` converter service.

Three agent loops meant three places to fix every tool-use, streaming,
HITL, DLP and quota bug, three sets of tests, and two sandbox image
families (`base` / `bootstrap` / `overlay` / `office` / `matlab` for
AgentScope, `claude-sdk*` for the TypeScript worker). In practice
production traffic had already converged on the Claude SDK worker: it
carries the newer features (HITL question/confirm cards, structured output,
resume context, file-based secret delivery, reasoning-effort mapping) and
the Cube templates in use are all `claude-sdk*` images.

## Decision drivers

1. **One agent loop to maintain.** Every cross-cutting concern (DLP,
   HITL, event translation, quota, egress, secrets) is implemented once.
2. **Isolation by default.** The inline mode executed tools inside the
   orchestrator process, outside the sandbox boundary that ADR 0001 exists
   to provide. Removing it means every run is sandboxed.
3. **Feature velocity.** The Claude Agent SDK brings built-in file, Bash,
   sub-agent and skill handling that we were re-implementing in AgentScope
   toolkits.
4. **Gateway neutrality is preserved.** Model choice stays a gateway
   concern: the worker's in-process adapter (`worker-claude-sdk/src/adapter.ts`)
   translates Anthropic Messages ⇄ the foundation LLM gateway's
   OpenAI-compatible API, so tenants are not locked to one model vendor.

## Decision

* **The Claude Agent SDK worker is the only agent runtime.** The sandbox
  entrypoint is `node /app/dist/main.js`, shipped in
  `ghcr.io/agent-platform/claude-sdk-sandbox:0.1` (built from
  `sandbox-images/claude-sdk.Dockerfile`) and the Cube-native
  `claude-sdk-cube*.Dockerfile` variants.
* **Removed:** `agentscope-ext/` and all its extensions; the
  `base` / `bootstrap` / `overlay` / `office` / `matlab` sandbox Dockerfiles
  and pre-fetched `wheels/`; the scheduler's per-run worker routing
  (`enable_claude_sdk_worker`) and the settings `SCHEDULER_CLAUDE_SDK_IMAGE`,
  `USE_CLAUDE_SDK_WORKER_DEFAULT`, `LLM_API_KEY`, `LLM_PROVIDER`,
  `LLM_BASE_URL`, `RUNTIME_EXTENSIONS`. `AGENT_IMAGE` /
  `SCHEDULER_AGENT_IMAGE` now point at the Claude SDK image.
* **Inline chat mode is removed.** Every run is a sandboxed Claude SDK
  run. `execution_mode` is still stored on runs so legacy rows remain
  readable; an incoming `inline` is coerced to `sandbox`, and the BFF no
  longer forwards the field. The `doc-to-md` service is removed from
  compose.
* **All model traffic goes through the foundation LLM gateway** with a
  per-tenant virtual key. A missing virtual key fails the spawn — there is
  no direct-provider fallback.
* **Sandbox token scopes drop `audit.write`.** Nothing inside the sandbox
  writes to foundation audit any more; run events still flow back over
  Redis to the orchestrator (transcript, session replay).
* **Knowledge is prefetched.** The orchestrator queries the tenant
  knowledge base and appends the top-k hits to the worker's system prompt.
* `sandbox-pool` pre-warms the Claude SDK image.

## Consequences

### Positive

* One worker, one image family, one wire contract
  ([contracts/worker-wire-contract.md](../../contracts/worker-wire-contract.md)).
* No model-generated tool calls execute outside a sandbox.
* Smaller configuration surface; no provider API keys anywhere in the
  agent stack.

### Known gaps vs. the AgentScope worker

These were provided by `agentscope-ext` and have **no equivalent yet** in
the Claude SDK worker:

| Gap | Was | Today |
|---|---|---|
| Agent-created canvases | `canvas_tool` let the agent create / revise a canvas mid-run | Users can still create and edit canvases; the agent cannot. Artifacts written to the workspace still surface as cards. |
| On-demand knowledge search | `knowledge_tool` let the agent query the KB as a tool, repeatedly, with its own queries | One prefetch of top-k hits per run, appended to the system prompt; the agent cannot issue follow-up queries. |
| In-sandbox audit sink | `event_audit_sink` wrote run events to foundation audit from inside the sandbox | No sandbox-originated audit records. Run events are still persisted platform-side for transcript / session replay; wiring them into foundation audit is a platform-side follow-up. |

Closing these is follow-up work (most naturally as MCP tools served by the
platform through `tool-broker`), and each should land with its own design
note.

### Neutral

* The gateway / adapter path is kept: the SDK talks to
  `http://localhost:<port>` (`ANTHROPIC_BASE_URL`), the adapter forwards to
  `LLM_GATEWAY_URL` with the three gateway headers. Swapping model vendors
  remains a gateway configuration change.
* ADR 0001 (CubeSandbox default, Kata fallback) is unaffected; the
  Claude SDK images are plain OCI images.
* Historical design docs that describe AgentScope remain as a record of
  how the platform evolved; they are marked historical where they would
  otherwise mislead.

### Negative

* The B-M3 SWE-bench target in ADR 0002 now applies to the Claude SDK
  worker + gateway combination; tool-use quality now depends on how well
  the adapter maps non-Anthropic models through the gateway.
* Dependency on an SDK whose terms are set by Anthropic (see `NOTICE`).
* Operators that relied on inline mode for low-latency chat now pay a
  sandbox spawn per run; the pre-warmed pool is the mitigation.

## Rollback

There is no runtime switch back. The AgentScope worker, its images and the
inline mode are recoverable from git history (the commit preceding this
ADR); restoring them is a revert plus re-adding the removed settings, not a
configuration change.
