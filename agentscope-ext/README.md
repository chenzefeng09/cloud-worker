# agentscope-ext — AgentScope extension modules

The agent-platform deliberately **does not fork** AgentScope (top-plan
risk #3 — upstream drift becomes a maintenance tax).  Instead we ship
our integration as standalone Python packages that the sandbox image
``pip install``s alongside the pinned AgentScope version, and that
``runtime_worker`` wires together at agent startup.

| Module | Purpose | Status |
|---|---|---|
| ``agentscope_ext/runtime_worker/`` | Sandbox entrypoint — boots a single ``ReActAgent`` with the configured ``Toolkit``, runs one task, persists the result + audit trail to the workspace, and exits.  This is what ``agent-scheduler`` invokes inside every sandbox container/VM. | B-M2 (this milestone) |
| ``agentscope_ext/event_audit_sink/`` | ``post_print`` hook that forwards every agent event to ``foundation.audit``. | Planned (B-M3) |
| ``agentscope_ext/llm_gateway_enforcer/`` | Custom ``ModelBase`` impl that routes every LLM call through ``foundation.llm-gateway`` (no direct provider keys in the sandbox). | Planned (B-M3) |
| ``agentscope_ext/mcp_tenant_injector/`` | Resolves the tenant's MCP-server list from ``mcp-registry`` at session start and feeds it into the ``Toolkit``. | Planned (B-M3) |
| ``agentscope_ext/skills_loader/`` | Materialises packaged Skills into the AgentScope ``Toolkit`` before run. | Planned (B-M3) |

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
``agentscope_ext/runtime_worker/__main__.py`` for the contract.

## Hook surface (the override points the four extensions plug into)

``runtime_worker`` exposes four module-level functions in
``agentscope_ext.runtime_worker.hooks``.  Each sibling extension owns
one (or two) of them and monkey-patches at import time:

| Hook function | Default behaviour | Owner extension (planned) |
|---|---|---|
| ``build_model(task, env) -> ModelBase`` | ``AnthropicChatModel`` or ``OpenAIChatModel`` depending on ``LLM_PROVIDER`` env var; reads ``LLM_API_KEY`` directly. | ``llm_gateway_enforcer`` — returns a ``GatewayModel`` that POSTs to ``foundation.llm-gateway`` with the foundation JWT instead of holding raw provider keys. |
| ``build_formatter(task, env) -> FormatterBase`` | Matches the provider above (``AnthropicChatFormatter`` / ``OpenAIChatFormatter``). | ``llm_gateway_enforcer`` (paired with the model). |
| ``build_toolkit(task, tenant_id, env) -> Toolkit`` | Empty ``Toolkit()`` — useful for smoke tests without the foundation stack. | ``mcp_tenant_injector`` then ``skills_loader`` (chained: MCP servers resolved from ``mcp-registry`` first, packaged Skills layered on top). |
| ``attach_hooks(agent, *, events_path, env) -> None`` | Registers a ``post_print`` instance hook that appends each printed ``Msg`` to ``/workspace/events.jsonl`` as one JSON line. | ``event_audit_sink`` — registers a **second** ``post_print`` hook that POSTs each event to ``foundation.audit``.  Both run; the disk log survives gateway outages, the audit POST survives sandbox crashes. |

Extensions install themselves by importing the hooks module and
reassigning the attribute, e.g.::

    from agentscope_ext.runtime_worker import hooks
    from agentscope_ext.llm_gateway_enforcer import build_gateway_model

    hooks.build_model = build_gateway_model

Because the entrypoint resolves the functions via attribute access on
each invocation (``_hooks.build_model(...)``), patching can happen at
sandbox-image build time (via a ``sitecustomize.py`` or a
``[project.entry-points."agentscope_ext.boot"]`` hook the scheduler
triggers) or at process start.  The runtime_worker itself never imports
the sibling packages directly — that keeps the dependency arrow
one-way and lets the smoke-test image ship without them.

## Versioning

The sandbox image pins a specific AgentScope minor (target: 2.0.x) and
bumps quarterly.  Extensions target the public AgentScope 2.0 surface —
``ReActAgent`` / ``Toolkit`` / ``MsgHub`` / formatter + model classes —
which AgentScope's release notes flag as stable.

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

## Tests

The wire-contract tests stub AgentScope via ``sys.modules`` so they run
without the real dependency::

    cd agentscope-ext
    python -m pytest tests/ -v
