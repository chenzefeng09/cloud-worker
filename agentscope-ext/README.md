# agentscope-ext — AgentScope extension modules

The agent-platform deliberately **does not fork** AgentScope (top-plan
risk #3 — upstream drift becomes a maintenance tax).  Instead we ship
our integration as standalone Python packages that the sandbox image
``pip install``s alongside the pinned AgentScope version, and that
``runtime_worker`` wires together at agent startup.

| Module | Purpose | Status |
|---|---|---|
| ``runtime_worker/`` | Sandbox entrypoint — boots a single ``ReActAgent`` with the configured ``Toolkit``, runs one task, persists the result + audit trail to the workspace, and exits.  This is what ``agent-scheduler`` invokes inside every sandbox container/VM. | Skeleton (B-M2) |
| ``event_audit_sink/`` | ``MsgHub`` subscriber that forwards every agent event (message, tool call, model invocation) to ``foundation.audit``. | Planned (B-M3) |
| ``llm_gateway_enforcer/`` | Custom ``ModelBase`` impl that routes every LLM call through ``foundation.llm-gateway`` (no direct provider keys in the sandbox). | Planned (B-M3) |
| ``mcp_tenant_injector/`` | Resolves the tenant's MCP-server list from ``mcp-registry`` at session start and feeds it into ``MCPToolkit``. | Planned (B-M3) |
| ``skills_loader/`` | Materialises Skills package into the AgentScope ``Toolkit`` before run. | Planned (B-M3) |

## How the sandbox picks these up

The packages are installed into the sandbox image (``sandbox-images/base.Dockerfile``)
alongside AgentScope itself.  The scheduler then invokes::

    python -m agentscope_ext.runtime_worker

The worker reads task + tenant context from the foundation-issued env
vars (``FOUNDATION_API_TOKEN``, ``FOUNDATION_TENANT_ID``, ...) and the
``/workspace/task.json`` file the scheduler dropped in, builds the
``ReActAgent`` with the tenant's MCP servers + Skills, runs the task,
and exits.  Container exit code + ``/workspace/result.json`` are how
the scheduler learns the outcome — see
``runtime_worker/__main__.py`` for the contract.

## Versioning

The sandbox image pins a specific AgentScope minor (target: 2.0.x) and
bumps quarterly.  Extensions target the public AgentScope 2.0 surface —
``ReActAgent`` / ``Toolkit`` / ``MCPToolkit`` / ``MsgHub`` — which
AgentScope's release notes flag as stable.

## Why not run AgentScope's ``examples/agent_service``?

AgentScope ships a multi-tenant FastAPI example.  We don't use it
directly because:

* **Tenant model is already ours.**  BFF + orchestrator own session /
  message / run / RLS — the example service overlaps.
* **One agent per sandbox is the security boundary.**  Long-running
  multi-tenant services in one process defeat the microVM isolation
  we pay for in B-M2.
* **Per-run lifecycle is the unit of audit + billing.**  A one-shot
  worker maps cleanly onto ``run`` rows; a long-running service blurs
  the line.
