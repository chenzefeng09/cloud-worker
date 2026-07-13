"""Inject a ``create_canvas`` tool into the agent's ``Toolkit`` (canvas v2, Phase B).

Wraps ``hooks.build_toolkit`` so it first delegates to whatever the previous
binding produced, then appends a :class:`~agentscope_ext.canvas_tool.tool.CreateCanvasTool`
to the basic tool group.  The tool writes canvas payloads to the run's Redis
hot-state (via a :class:`RunStateClient` built from env, exactly as the runtime
worker does), which the orchestrator materializes into first-class canvas
documents.

Activation: gated by the ``canvas_tool`` name in ``RUNTIME_EXTENSIONS`` (the
scheduler appends it per-run behind the ``enable_canvas_tool`` feature flag).
Call :func:`install` exactly once; idempotent.  Register it after
``mcp_tenant_injector`` / ``skills_loader`` / ``web_fetch`` so all built-in
tools, MCP servers, and Skills are present before this tool is appended.

Flag off ⇒ the name is absent from ``RUNTIME_EXTENSIONS`` ⇒ ``install()`` is
never called ⇒ zero behaviour change (the model never sees ``create_canvas``).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    """Wrap ``hooks.build_toolkit`` to append :class:`CreateCanvasTool`.

    Idempotent — subsequent calls are no-ops.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.build_toolkit

    def _build_with_canvas_tool(
        task: dict, tenant_id: str, env: Mapping[str, str]
    ) -> Any:
        toolkit = previous(task, tenant_id, env)

        try:
            from agentscope_ext.canvas_tool.tool import CreateCanvasTool
            from agentscope_ext.runtime_worker import redis_state as _redis

            # Reuse the worker's run-scoped client (built BEFORE the env
            # scrub removed REDIS_URL / broker lease from os.environ).
            # build_toolkit runs after the scrub, so a fresh from_env() here
            # would silently yield a no-op client and every push_canvas would
            # return seq=0 — the payload would never reach Redis and the
            # canvas would never materialize.  from_env() stays as fallback
            # for harnesses that call build_toolkit without booting the worker.
            state_client = _redis.get_shared_client() or _redis.from_env()
            tool = CreateCanvasTool(state_client=state_client)
            toolkit.tool_groups[0].tools.append(tool)
            _LOG.info("canvas_tool: registered create_canvas tool")
        except Exception as exc:  # noqa: BLE001 -- never break boot
            _LOG.warning(
                "canvas_tool: failed to register create_canvas tool: %r", exc
            )

        return toolkit

    _hooks.build_toolkit = _build_with_canvas_tool


__all__ = ["install"]
