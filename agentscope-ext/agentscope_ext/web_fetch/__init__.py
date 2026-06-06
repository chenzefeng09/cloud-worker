"""Inject a WebFetch tool into the agent's ``Toolkit``.

Wraps ``hooks.build_toolkit`` so it first delegates to whatever the
previous binding produced, then (if ``WEB_FETCH_GATEWAY_URL`` is set in
the sandbox env) instantiates :class:`~agentscope_ext.web_fetch.tool.WebFetchTool`
and appends it to the basic tool group.

Activation: call :func:`install` exactly once; idempotent.  Register this
after ``mcp_tenant_injector`` and ``skills_loader`` in
``RUNTIME_EXTENSIONS`` so built-in tools, MCP servers, and packaged Skills
are all present before the WebFetch tool is appended.

Gateway contract
----------------
The gateway exposes a single ``POST /api/web-fetch/v1/fetch`` endpoint.
It enforces domain allow-listing, private-range blocking, and content-type
restrictions, so this extension only needs to call it — no network policy
enforcement here.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    """Wrap ``hooks.build_toolkit`` to append :class:`WebFetchTool`.

    Idempotent — subsequent calls are no-ops.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.build_toolkit

    def _build_with_web_fetch(
        task: dict, tenant_id: str, env: Mapping[str, str]
    ) -> Any:
        toolkit = previous(task, tenant_id, env)

        gateway_url = env.get("WEB_FETCH_GATEWAY_URL", "")
        if not gateway_url:
            _LOG.warning(
                "web_fetch: WEB_FETCH_GATEWAY_URL is not set — "
                "WebFetch tool will not be registered"
            )
            return toolkit

        try:
            from agentscope_ext.web_fetch.tool import WebFetchTool

            tool = WebFetchTool(
                gateway_url=gateway_url,
                token=env.get("FOUNDATION_API_TOKEN", ""),
                tenant_id=tenant_id,
                user_id=env.get("FOUNDATION_USER_ID", ""),
                service_scopes=env.get("FOUNDATION_SERVICE_SCOPES", ""),
            )
            toolkit.tool_groups[0].tools.append(tool)
            _LOG.info(
                "web_fetch: registered WebFetchTool (gateway=%s)", gateway_url
            )
        except Exception as exc:  # noqa: BLE001 -- never break boot
            _LOG.warning(
                "web_fetch: failed to register WebFetchTool: %r", exc
            )

        return toolkit

    _hooks.build_toolkit = _build_with_web_fetch


__all__ = ["install"]
