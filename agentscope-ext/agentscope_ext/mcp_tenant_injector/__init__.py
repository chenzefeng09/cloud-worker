"""Inject tenant-scoped MCP servers into the agent's ``Toolkit``.

Wraps ``hooks.build_toolkit`` so it first delegates to whatever the
previous binding produced (the default empty toolkit, or another
extension's wrapper), then asks ``foundation.mcp-registry`` for the
tenant's server list and registers each one.

Activation: call :func:`install` exactly once; idempotent.  When
chained with :mod:`agentscope_ext.skills_loader`, install this *first*
so MCP servers are visible to packaged Skills.

AgentScope 2.0 MCP API
-----------------------
``agentscope.mcp.MCPClient`` is the unified client.  It accepts:

* ``StdioMCPConfig(command=..., args=..., env=...)`` — stdio transport,
  **must** be stateful (``is_stateful=True``); requires an explicit
  ``await client.connect()`` before the client is passed to ``Toolkit``.
* ``HttpMCPConfig(url=..., headers=..., timeout=...)`` — HTTP/SSE
  transport; can be stateless (``is_stateful=False``), no connect needed.

MCPClient objects are registered by appending to the ``mcps`` list of the
Toolkit's ``"basic"`` ToolGroup (``toolkit.tool_groups[0].mcps``).  The
Toolkit iterates that list lazily when listing/calling tools, so stateful
clients that have been connected are fully usable immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

from . import injector

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.build_toolkit

    def _build_with_mcp(
        task: dict, tenant_id: str, env: Mapping[str, str]
    ) -> Any:
        toolkit = previous(task, tenant_id, env)
        try:
            # Prefer the dedicated MCP_REGISTRY_URL; fall back to FOUNDATION_API_URL
            # so the extension keeps working in legacy single-URL deployments.
            base_url = env.get("MCP_REGISTRY_URL") or env.get("FOUNDATION_API_URL", "")
            # FOUNDATION_SERVICE_SCOPES is injected by the scheduler into the
            # sandbox env.  In prod the API gateway sets X-Service-Scopes after
            # verifying the Bearer JWT; in dev (no gateway) this env var carries
            # the same value so the internal endpoint can authorise the call.
            service_scopes = env.get("FOUNDATION_SERVICE_SCOPES", "")
            servers = injector.fetch_mcp_servers(
                base_url=base_url,
                token=env.get("FOUNDATION_API_TOKEN", ""),
                tenant_id=tenant_id,
                service_scopes=service_scopes,
                broker_lease=env.get("TOOL_BROKER_LEASE", ""),
            )
        except Exception as exc:  # noqa: BLE001 -- never break boot
            _LOG.warning("mcp_tenant_injector: registry fetch failed: %r", exc)
            return toolkit

        for server in servers:
            try:
                prepared = _prepare_server_for_egress(server, env)
                if prepared is None:
                    continue
                _register_one(toolkit, prepared)
            except Exception as exc:  # noqa: BLE001 -- skip the bad one
                _LOG.warning(
                    "mcp_tenant_injector: register failed for %r: %r",
                    server.get("name"),
                    exc,
                )
        return toolkit

    _hooks.build_toolkit = _build_with_mcp


def _prepare_server_for_egress(
    server: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any] | None:
    """Return a server manifest reachable from the sandbox's egress mode.

    Broker-only sandboxes cannot resolve or connect to registry-supplied HTTP
    endpoints directly.  Route those MCP sessions through the tool-broker's
    tenant-scoped passthrough and attach the per-run capability headers.  Stdio
    servers remain local to the sandbox and need no rewrite.

    Missing broker routing information drops just this server (failure-open)
    instead of handing AgentScope an endpoint that is guaranteed to fail.
    """
    prepared = dict(server)
    transport = str(prepared.get("transport") or "stdio").lower()
    if env.get("SANDBOX_EGRESS_MODE") != "broker" or transport == "stdio":
        return prepared

    server_id = str(prepared.get("id") or "").strip()
    broker_base = str(env.get("TOOL_BROKER_URL") or "").rstrip("/")
    if not server_id or not broker_base:
        _LOG.warning(
            "mcp_tenant_injector: broker mode dropping MCP %r "
            "(missing server id / tool-broker url)",
            prepared.get("name"),
        )
        return None

    api_token = str(env.get("FOUNDATION_API_TOKEN") or "").strip()
    tenant_id = str(env.get("FOUNDATION_TENANT_ID") or "").strip()
    broker_lease = str(env.get("TOOL_BROKER_LEASE") or "").strip()
    if not api_token or not tenant_id or not broker_lease:
        _LOG.warning(
            "mcp_tenant_injector: broker mode dropping MCP %r "
            "(missing broker authentication context)",
            prepared.get("name"),
        )
        return None

    headers = dict(prepared.get("headers") or {})
    # Broker authority always wins over untrusted registry/header values.
    headers.update(
        {
            "Authorization": f"Bearer {api_token}",
            "X-Tenant-Id": tenant_id,
            "X-Tool-Broker-Lease": broker_lease,
        }
    )
    prepared["endpoint"] = f"{broker_base}/mcp-tenant/v1/{server_id}"
    prepared.pop("url", None)
    prepared["headers"] = headers
    return prepared


def _build_mcp_client(name: str, server: Mapping[str, Any]) -> Any:
    """Build an ``agentscope.mcp.MCPClient`` from a registry server spec.

    Transport mapping:
    * ``"stdio"``     → ``StdioMCPConfig``; ``is_stateful=True``
      (STDIO must be stateful per AgentScope 2.0 validation).
    * ``"http"``, ``"sse"``, ``"streamable_http"``, or any HTTP value
      → ``HttpMCPConfig``; ``is_stateful=False`` (stateless, no connect).

    The registry may provide ``endpoint`` or ``url`` for the HTTP URL.
    """
    from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig

    transport = server.get("transport", "stdio")

    if transport == "stdio":
        command = server.get("command")
        if not command:
            raise ValueError(f"stdio server {name!r} missing 'command'")
        config = StdioMCPConfig(
            command=command,
            args=server.get("args") or None,
            env=server.get("env") or None,
        )
        return MCPClient(name=name, is_stateful=True, mcp_config=config)
    else:
        # http / sse / streamable_http — use whichever URL field is present
        url = server.get("url") or server.get("endpoint")
        if not url:
            raise ValueError(
                f"http server {name!r} missing 'url'/'endpoint'"
            )
        config = HttpMCPConfig(
            url=url,
            headers=server.get("headers") or None,
        )
        return MCPClient(name=name, is_stateful=False, mcp_config=config)


async def _connect_and_probe(client: Any) -> None:
    """Connect a stateful client and probe tools; for stateless, probe only."""
    if client.is_stateful:
        await client.connect()
    # Probe tool listing so we catch unreachable / auth-failed servers NOW
    # rather than letting a broken client into the Toolkit where it crashes
    # the agent's reply_stream TaskGroup at runtime.
    await client.list_raw_tools()


def _register_one(toolkit: Any, server: Mapping[str, Any]) -> None:
    """Create an ``MCPClient`` from the registry spec and add it to the
    Toolkit's basic ToolGroup.

    Uses the AgentScope 2.0 API:
    * ``agentscope.mcp.MCPClient`` with ``StdioMCPConfig`` or
      ``HttpMCPConfig``.
    * All clients are probed (connect + list_raw_tools) via ``asyncio.run``
      before being appended.  This ensures unreachable / auth-failed servers
      are skipped rather than crashing the agent's reply_stream TaskGroup.
    * The client is appended to ``toolkit.tool_groups[0].mcps`` — the
      Toolkit's "basic" group — so AgentScope finds it during tool lookup.
    """
    name = server.get("name")
    if not name:
        raise ValueError("server manifest is missing 'name'")

    client = _build_mcp_client(name, server)

    # build_toolkit is a sync hook called before asyncio.run(_dispatch),
    # so we get a fresh event loop here via asyncio.run.
    asyncio.run(_connect_and_probe(client))
    _LOG.info("mcp_tenant_injector: probed MCP %r OK", name)

    # Append to the Toolkit's basic ToolGroup mcps list.
    basic_group = toolkit.tool_groups[0]
    basic_group.mcps.append(client)
    _LOG.info(
        "mcp_tenant_injector: registered MCP %r (transport=%s)",
        name,
        server.get("transport", "stdio"),
    )


__all__ = ["install"]
