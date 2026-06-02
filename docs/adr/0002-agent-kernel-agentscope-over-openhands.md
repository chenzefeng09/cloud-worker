# ADR 0002 — Agent kernel: AgentScope, not OpenHands

- **Status**: Accepted
- **Date**: 2026-06
- **Phase**: pre-B-M2 (decision belongs in B-M1's tail but lands here)
- **Supersedes**: implicit choice in top-plan revision 1 (OpenHands)
- **Plan reference**: top-plan [§ 决策 — Agent 内核（B 阶段）] + [§ B.1 仓库结构 — `agentscope-ext/`]

## Context

The top-plan originally pinned **OpenHands** (All-Hands-AI) as the agent
kernel.  Reasons in the original entry: best public SWE-bench scores,
ships its own MCP / Runtime / Web UI, batteries-included platform.

During B-M1's tail we reconsidered and switched to
**[AgentScope 2.0](https://github.com/agentscope-ai/agentscope)** (阿里通义
开源, Apache 2.0).  This ADR records *why* and *what changes downstream*
so future contributors don't read the (now-amended) plan and assume the
old choice was wrong, just unsuitable for our shape.

## Decision drivers

1. **Architecture fit.**  OpenHands is a *platform* (runtime + UI + state
   machine + chat protocol).  AgentScope is a *framework* (ReActAgent +
   Toolkit + MsgHub + Model abstraction).  We already own the platform
   layer — BFF, session-orchestrator, sandbox-pool, foundation auth /
   audit / billing.  A framework slots into that without overlapping;
   a platform forces us to either disable half of OpenHands or fork it.
2. **License.**  AgentScope is **Apache 2.0** — no commercial-use
   restrictions, no copyleft.  OpenHands is dual MIT + Polyform-enterprise;
   the Polyform clause restricts hosted-commercial use of certain
   components and would require legal review per private-cloud customer.
3. **MCP support.**  Both have first-class MCP.  Tie.
4. **Multi-agent orchestration.**  AgentScope ships ``MsgHub`` for
   message-passing between agents — we'll need this for the "三层 agent"
   pattern (per top-plan, Manus-style) without writing our own.
   OpenHands is single-agent by default.
5. **Operational maturity.**  AgentScope 2.0 dropped 2026-05-25 with
   v2.0.0 as a clean public release; 26k+ stars; backed by 阿里通义
   团队 with a multi-year track record on AgentScope 1.x.  Project age
   risk is real (B-M2 risk #4 already manages similar risk for
   CubeSandbox) but less acute than for new-and-niche frameworks.
6. **Existing investment to discard.**  B-M1 already shipped the
   sandbox + scheduler + token-mint plumbing with OpenHands as a
   thin assumption.  Swapping costs ~20 file edits (mechanical) +
   1 design doc + the new ``runtime_worker`` entrypoint.  Confirmed
   small enough to do now rather than carry an unwanted dependency.

## Decision

* **Agent kernel: AgentScope 2.x.**  Pinned to a known-good minor in
  the sandbox image; bumped quarterly via ``sandbox-images/`` rebuild.
* **Integration shape: one agent per sandbox, one task per agent.**
  Sandbox container runs ``python -m agentscope_ext.runtime_worker``;
  the worker builds a fresh ``ReActAgent + Toolkit`` from the per-run
  task payload, runs the task, writes ``result.json``, exits.  Long-running
  AgentScope ``examples/agent_service`` is **not** used — would defeat
  our microVM-per-tenant isolation boundary.
* **Extensions live in ``agentscope-ext/``** (renamed from
  ``openhands-ext/``).  Five modules; see that directory's README.
  Not a fork — independent Python packages installed into the sandbox
  image alongside AgentScope itself.
* **Frontend**: stays the React app we built in B-M1.  AgentScope ships
  ``examples/web_ui`` but it overlaps our BFF / chat-stream API; we
  borrow patterns, not code.

## Mapping table (for the agent's-eye view of what changed)

| Concern | OpenHands (was) | AgentScope (now) |
|---|---|---|
| Sandbox entry | ``python -m openhands.core.main -f config.toml -t task`` | ``python -m agentscope_ext.runtime_worker`` |
| Task delivery  | ``-t`` argv | ``/workspace/task.json`` (docker) or ``AGENT_TASK_JSON`` env (cube) |
| Agent class    | ``CodeActAgent`` (built-in) | ``agentscope.agents.ReActAgent`` (our wiring) |
| Tool layer     | OpenHands plugins / functions | ``agentscope.toolkit.Toolkit`` |
| MCP wiring     | ``mcp_tenant_injector/`` → OpenHands plugin registry | ``mcp_tenant_injector/`` → ``MCPToolkit`` |
| LLM gateway    | ``llm_gateway_enforcer/`` → OpenHands LLM client | ``llm_gateway_enforcer/`` → custom ``ModelBase`` |
| Event capture  | EventStream subscriber | ``MsgHub`` subscriber |
| Web UI         | OpenHands frontend (fork) | Self-written React (B-M1 ✅) |
| Image          | ``ghcr.io/all-hands-ai/openhands:0.27`` | ``ghcr.io/agent-platform/agentscope-sandbox:0.1`` (we build) |

## Risk management

| Risk | Signal | Response |
|---|---|---|
| AgentScope 2.x breaks API in a minor (project is young) | Quarterly upgrade smoke test fails | Pin to last-good version; cherry-pick AgentScope commits; in extremis, fork |
| ``ReActAgent`` falls behind SOTA on tool-use benchmarks | Our SWE-bench-subset score (top-plan B-M3 验证) drops | Swap to a heavier agent class (AgentScope ``Pipeline`` or custom); the runtime_worker boundary makes this a local change |
| ``MCPToolkit`` differs from MCP spec in ways tenants notice | Conformance check + customer ticket | Patch in ``mcp_tenant_injector/`` adapter |
| We need OpenHands' SWE-bench score back | New evidence that AgentScope materially underperforms on our customer's workloads | Re-evaluate via ADR amendment; runtime_worker can host either kernel because the contract is `task.json → result.json` |

## Consequences

* B-M2 deliverable list (per top-plan B.2) is unchanged in scope but
  the wording shifts: "OpenHands extensions 五个" → "AgentScope
  extensions 五个 (agentscope-ext/)".  Plan was amended in this
  revision.
* B-M3 acceptance criterion ("agent 能力基线 SWE-bench Verified 100 题
  >65%") still applies; the benchmark target is the *system* not the
  framework.  If AgentScope's ReActAgent can't hit 65% with our tools
  + LLM gateway we owe an ADR amendment + a different agent class.
* The `runtime_worker` contract (``task.json`` in, ``result.json`` out,
  exit codes) is now load-bearing.  Two runtimes (docker, cube)
  depend on it.  Changing it is a coordinated change across three
  packages.

## Follow-ups tracked

* Build ``sandbox-images/base.Dockerfile`` that installs AgentScope
  2.x + agentscope_ext (B-M2 / M4 in current task list).
* Real ``ReActAgent`` wiring inside ``runtime_worker._run_skeleton`` —
  currently echoes the task.  Owner: B-M2 lead.
* Quarterly AgentScope upgrade routine — owner Platform team, first
  run 2026-09.
* Re-evaluate after AgentScope 2.1 or 2.2 lands (whichever is sooner)
  whether our integration surface needs to widen.
