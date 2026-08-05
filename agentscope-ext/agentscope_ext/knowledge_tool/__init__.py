"""Inject a ``search_knowledge`` tool into the agent's ``Toolkit`` (KB v1).

Wraps ``hooks.build_toolkit`` so it first delegates to whatever the previous
binding produced, then (if ``KNOWLEDGE_INGEST_URL`` is set in the sandbox env)
instantiates :class:`~agentscope_ext.knowledge_tool.tool.SearchKnowledgeTool`
and appends it to the basic tool group.

This mirrors the **inline** path's ``search_knowledge`` contract
(``session-orchestrator/.../inline_knowledge_client.py``) but runs inside the
sandbox: the model can proactively query the tenant knowledge base even on the
default (sandbox) execution path.  The tool calls ki ``GET /search?scope=knowledge``
over HTTP with the three identity headers derived from the sandbox env
(``FOUNDATION_TENANT_ID`` / ``FOUNDATION_USER_ID`` / ``FOUNDATION_USER_CLEARANCE``).
In broker egress mode ``KNOWLEDGE_INGEST_URL`` points at the tool-broker
``/knowledge/v1`` façade and the worker additionally sends ``X-Tool-Broker-Lease``
(the broker re-injects the identity headers from the signed lease).

Activation: gated by the ``knowledge_tool`` name in ``RUNTIME_EXTENSIONS`` (the
scheduler includes it by default).  Call :func:`install` exactly once;
idempotent.  Register it after ``mcp_tenant_injector`` / ``skills_loader`` /
``web_fetch`` so all built-in tools, MCP servers, and Skills are present before
this tool is appended.

``KNOWLEDGE_INGEST_URL`` unset (e.g. ``none`` egress mode, or a deployment
without ki) ⇒ the tool is not registered and the model never sees
``search_knowledge`` - fail-open, exactly like the inline path
(``inline_run_executor.py``) when ki is unreachable.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    """Wrap ``hooks.build_toolkit`` to append :class:`SearchKnowledgeTool`.

    Idempotent - subsequent calls are no-ops.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.build_toolkit

    def _build_with_knowledge_tool(
        task: dict, tenant_id: str, env: Mapping[str, str]
    ) -> Any:
        toolkit = previous(task, tenant_id, env)

        base_url = env.get("KNOWLEDGE_INGEST_URL", "")
        if not base_url:
            _LOG.info(
                "knowledge_tool: KNOWLEDGE_INGEST_URL is not set - "
                "search_knowledge tool will not be registered"
            )
            return toolkit

        try:
            from agentscope_ext.knowledge_tool.tool import SearchKnowledgeTool

            tool = SearchKnowledgeTool(
                base_url=base_url,
                tenant_id=env.get("FOUNDATION_TENANT_ID", ""),
                user_id=env.get("FOUNDATION_USER_ID", ""),
                clearance=env.get("FOUNDATION_USER_CLEARANCE", ""),
                broker_lease=env.get("TOOL_BROKER_LEASE", ""),
            )
            toolkit.tool_groups[0].tools.append(tool)
            _LOG.info(
                "knowledge_tool: registered search_knowledge tool (base=%s)",
                base_url,
            )
        except Exception as exc:  # noqa: BLE001 -- never break boot
            _LOG.warning(
                "knowledge_tool: failed to register search_knowledge tool: %r",
                exc,
            )

        return toolkit

    _hooks.build_toolkit = _build_with_knowledge_tool


__all__ = ["install"]
