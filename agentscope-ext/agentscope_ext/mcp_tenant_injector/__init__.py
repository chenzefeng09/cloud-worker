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
import time
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
        # The run-level MCP selection rides in task.json, which this hook already
        # receives as its first argument (runtime_worker/__main__.py:1231 passes
        # the parsed payload straight through) - no env channel needed.
        selected_ids = _selected_server_ids(task)
        # Explicit empty selection = MCP is off for this run.  Short-circuit
        # before the registry call; nothing could survive the filter anyway.
        # ``is not None`` is deliberate: ``[]`` is falsy, and a truthiness test
        # here would silently mean "tenant default" instead of "disable all".
        if selected_ids is not None and not selected_ids:
            _LOG.info(
                "mcp_tenant_injector: MCP disabled for this run "
                "(mcp_server_ids=[])"
            )
            return toolkit
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

        # Run-level selection is applied HERE - before _prepare_server_for_egress
        # (transport support / broker rewrite) and before _register_one (which
        # connects and probes the server, and may fetch an OAuth token). The two
        # filters answer different questions and must not be interleaved:
        #   selection = "did the user pick this server for this run?" (identity)
        #   transport = "can this runtime actually speak to it?"      (capability)
        # Selecting first guarantees a deselected server is never prepared and,
        # more importantly, never contacted; it also keeps the "skipping SSE
        # server" warnings meaningful, since they now only fire for servers the
        # user actually asked for.
        if selected_ids is not None:
            wanted = set(selected_ids)
            total = len(servers)
            servers = [
                s for s in servers if str(s.get("id") or "").strip() in wanted
            ]
            _LOG.info(
                "mcp_tenant_injector: mcp_server_ids selection: %d/%d "
                "server(s) kept",
                len(servers),
                total,
            )

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


def _selected_server_ids(task: Mapping[str, Any]) -> list[str] | None:
    """Read the run-level MCP selection from ``task.json``, PRESERVING presence.

    ``task["mcp_server_ids"]`` is three-state and the distinction is
    load-bearing, so this is a presence check (``key in task``) and never a
    truthiness check -- ``[]`` is falsy but means the opposite of absent:

    * key absent  -> ``None``: use the tenant's default enabled set (the
      historical "register every enabled server" behaviour).
    * ``[]``      -> ``[]``: MCP is disabled for this run; register nothing.
    * non-empty   -> those ids: register ONLY servers with a matching ``id``.

    A present-but-malformed value (not a list) degrades to ``None`` rather than
    to "disable everything": a scheduler shipping a bad shape falls back to the
    pre-existing behaviour instead of silently stripping the user's tools.

    Note this is NOT ``task["mcp_servers"]`` -- that is a legacy key the
    scheduler hardcodes to ``[]`` (docker_runtime.py:929) because servers are
    fetched live from the registry.
    """
    if not isinstance(task, Mapping) or "mcp_server_ids" not in task:
        return None
    raw = task.get("mcp_server_ids")
    if not isinstance(raw, list):
        return None
    return [str(v).strip() for v in raw if isinstance(v, str) and str(v).strip()]


def _prepare_server_for_egress(
    server: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any] | None:
    """Return a server manifest reachable from the sandbox's egress mode.

    ``sse`` servers use the legacy dual-endpoint SSE transport (``GET /sse`` +
    ``POST``) which the agentscope ``HttpMCPConfig`` does not speak (it is
    Streamable HTTP only).  They are skipped here with a warning - only the
    SDK worker direct path supports legacy SSE natively.

    Broker-only sandboxes cannot resolve or connect to registry-supplied HTTP
    endpoints directly.  Route those MCP sessions through the tool-broker's
    tenant-scoped passthrough and attach the per-run capability headers.  Stdio
    servers remain local to the sandbox and need no rewrite.

    Missing broker routing information drops just this server (failure-open)
    instead of handing AgentScope an endpoint that is guaranteed to fail.
    """
    prepared = dict(server)
    transport = str(prepared.get("transport") or "stdio").lower()
    if transport == "sse":
        _LOG.warning(
            "mcp_tenant_injector: skipping SSE server %r (legacy SSE not "
            "supported via agentscope injector; use worker direct mode)",
            prepared.get("name"),
        )
        return None
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

    # The broker auth headers below are for the BROKER (its _require_auth +
    # _require_lease gates), NOT for the upstream. Registry-supplied upstream
    # ``auth`` / ``headers`` are intentionally NOT forwarded: the broker is the
    # single upstream-auth injection point for broker-routed sessions (it reads
    # them from the registry record and injects them toward the real upstream),
    # so sending them here would only hand duplicates to the broker. Drop
    # ``auth`` outright; replace ``headers`` with broker-auth only. ``args`` /
    # ``tool_filter`` / ``timeout_seconds`` are NOT transport-level HTTP fields
    # and stay on the record for _build_mcp_client to apply client-side.
    prepared["endpoint"] = f"{broker_base}/mcp-tenant/v1/{server_id}"
    prepared.pop("url", None)
    prepared.pop("auth", None)
    prepared["headers"] = {
        "Authorization": f"Bearer {api_token}",
        "X-Tenant-Id": tenant_id,
        "X-Tool-Broker-Lease": broker_lease,
    }
    return prepared


def _auth_to_headers(auth: Any) -> dict[str, str]:
    """Convert a registry ``auth`` record into HTTP headers for the upstream.

    Only meaningful in DIRECT (non-broker) HTTP mode: broker-routed sessions
    have the broker inject upstream auth, so by the time _build_mcp_client runs
    on a broker-routed record the ``auth`` field has already been popped by
    _prepare_server_for_egress and this returns {}.
    """
    if not isinstance(auth, dict):
        return {}
    out: dict[str, str] = {}
    auth_type = str(auth.get("type") or "none").lower()
    if auth_type == "bearer":
        token = str(auth.get("token") or "").strip()
        if token:
            out["Authorization"] = f"Bearer {token}"
    elif auth_type == "api_key":
        api_key = str(auth.get("api_key") or "").strip()
        if api_key:
            header_name = str(auth.get("header_name") or "").strip() or "X-API-Key"
            out[header_name] = api_key
    return out


# ---------------------------------------------------------------------------
# OAuth client-credentials token cache (direct-mode MCP servers)
# ---------------------------------------------------------------------------
#
# In DIRECT (non-broker) HTTP mode the injector injects upstream auth itself.
# For ``auth.type == "oauth"`` it performs the client-credentials flow (POST to
# the registry-supplied ``token_url``), caches the access_token keyed by
# ``server_id``, and injects ``Authorization: Bearer {access_token}``.
#
# The injector registers servers SEQUENTIALLY (one ``asyncio.run`` per server in
# ``_register_one``), so there is never a concurrent fetch for the same
# ``server_id`` and a single-flight lock is not required here (unlike the
# long-lived broker / inline services, which use ``asyncio.Lock``).  The
# module-level cache still prevents a refetch on a valid token within one
# process.  A fetch failure is fail-open (logged + no Authorization header) so
# the MCPClient probe surfaces the upstream's own 401 and the server is skipped.
_OAUTH_TOKEN_CACHE: dict[str, dict[str, object]] = {}
_OAUTH_REFRESH_MARGIN_SECONDS = 60
_OAUTH_TOKEN_TIMEOUT_SECONDS = 10.0


def _fetch_oauth_token(auth: Any) -> tuple[str, float]:
    """POST ``client_credentials`` to ``auth.token_url`` -> (access_token, expires_at).

    ``expires_at`` is a unix-epoch seconds timestamp.  ``expires_in`` defaults to
    3600s when the token endpoint omits it or returns a non-numeric value.
    Raises on any transport / HTTP / shape error so the caller can fail-open.
    """
    import httpx  # local import - only needed for oauth

    if not isinstance(auth, dict):
        raise ValueError("oauth auth must be a mapping")
    token_url = str(auth.get("token_url") or "").strip()
    client_id = str(auth.get("client_id") or "").strip()
    client_secret = str(auth.get("client_secret") or "").strip()
    if not token_url or not client_id or not client_secret:
        raise ValueError("oauth auth requires token_url, client_id, client_secret")
    scopes_raw = auth.get("scopes")
    scopes = (
        [str(s) for s in scopes_raw if str(s)]
        if isinstance(scopes_raw, list)
        else []
    )
    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scopes:
        form["scope"] = " ".join(scopes)
    resp = httpx.post(token_url, data=form, timeout=_OAUTH_TOKEN_TIMEOUT_SECONDS)
    resp.raise_for_status()
    body = resp.json()
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("oauth token endpoint returned no access_token")
    try:
        expires_in = float(body.get("expires_in"))
    except (TypeError, ValueError):
        expires_in = 3600.0
    return access_token, time.time() + expires_in


def _get_oauth_token(server_id: str, auth: Any) -> str:
    """Return a cached oauth access_token for ``server_id``, refreshing on expiry.

    No single-flight lock: the injector registers servers sequentially (one
    ``asyncio.run`` per server), so there is no concurrent fetch for the same
    ``server_id``; the module-level cache prevents a refetch on a valid token.
    """
    cached = _OAUTH_TOKEN_CACHE.get(server_id)
    now = time.time()
    if (
        cached
        and float(cached.get("expires_at", 0.0)) - now
        > _OAUTH_REFRESH_MARGIN_SECONDS
    ):
        return str(cached["access_token"])
    access_token, expires_at = _fetch_oauth_token(auth)
    _OAUTH_TOKEN_CACHE[server_id] = {
        "access_token": access_token,
        "expires_at": expires_at,
    }
    return access_token


def _parse_tool_filter(
    raw: Any,
) -> tuple[list[str] | None, list[str] | None]:
    """Split a registry ``tool_filter`` into (enable, disable) lists.

    enable = allow with block subtracted (so enable/disable never overlap -
    AgentScope's MCPClient raises ValueError on overlap, which would otherwise
    drop the whole server). A tool in both allow and block ends up disabled,
    matching the contract (allow first, then block).
    """
    if not isinstance(raw, dict):
        return None, None
    allow_raw = raw.get("allow") if isinstance(raw.get("allow"), list) else []
    block_raw = raw.get("block") if isinstance(raw.get("block"), list) else []
    allow = [str(t) for t in allow_raw if str(t)]
    block = [str(t) for t in block_raw if str(t)]
    if not allow and not block:
        return None, None
    block_set = set(block)
    enable = [t for t in allow if t not in block_set] if allow else None
    disable = block or None
    return enable, disable


def _build_mcp_client(name: str, server: Mapping[str, Any]) -> Any:
    """Build an ``agentscope.mcp.MCPClient`` from a registry server spec.

    Transport mapping:
    * ``"stdio"``     → ``StdioMCPConfig``; ``is_stateful=True``
      (STDIO must be stateful per AgentScope 2.0 validation).
    * ``"http"`` / ``"streamable_http"`` / any HTTP value (NOT ``"sse"`` -
      legacy SSE is dropped by ``_prepare_server_for_egress`` because the
      agentscope HTTP client only speaks Streamable HTTP; the SDK worker
      direct path supports SSE natively)
      → ``HttpMCPConfig``; ``is_stateful=False`` (stateless, no connect).

    The registry may provide ``endpoint`` or ``url`` for the HTTP URL.
    """
    from agentscope.mcp import MCPClient, StdioMCPConfig, HttpMCPConfig

    transport = server.get("transport", "stdio")
    # tool_filter -> MCPClient enable_tools (allow) / disable_tools (block).
    # AgentScope applies these inside list_raw_tools so the probe AND the
    # toolkit see the filtered set (allow first, then block).
    enable_tools, disable_tools = _parse_tool_filter(server.get("tool_filter"))

    # timeout_seconds: HTTP gets it as the request timeout (connect+read); both
    # transports get it as per-tool execution_timeout (the only knob for stdio,
    # since StdioMCPConfig has no timeout field).
    raw_timeout = server.get("timeout_seconds")
    timeout_seconds = (
        float(raw_timeout)
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0
        else None
    )
    exec_timeout_kw: dict[str, Any] = (
        {"execution_timeout": timeout_seconds} if timeout_seconds is not None else {}
    )

    if transport == "stdio":
        command = server.get("command")
        if not command:
            raise ValueError(f"stdio server {name!r} missing 'command'")
        config = StdioMCPConfig(
            command=command,
            args=server.get("args") or None,
            env=server.get("env") or None,
        )
        return MCPClient(
            name=name,
            is_stateful=True,
            mcp_config=config,
            enable_tools=enable_tools,
            disable_tools=disable_tools,
            **exec_timeout_kw,
        )
    else:
        # http / sse / streamable_http — use whichever URL field is present
        url = server.get("url") or server.get("endpoint")
        if not url:
            raise ValueError(
                f"http server {name!r} missing 'url'/'endpoint'"
            )
        # Merge custom headers + auth-derived headers (auth only present in
        # direct mode; broker mode popped it). Auth headers win on collision.
        # For ``oauth`` the injector performs the client-credentials flow itself
        # (cached by server_id) and injects the bearer; a fetch failure is
        # fail-open (logged + no Authorization header) so the probe surfaces the
        # upstream's own 401 and the server is skipped.
        headers = dict(server.get("headers") or {})
        auth = server.get("auth")
        if isinstance(auth, dict) and str(auth.get("type") or "none").lower() == "oauth":
            oauth_server_id = str(server.get("id") or "").strip()
            if oauth_server_id:
                try:
                    access_token = _get_oauth_token(oauth_server_id, auth)
                    headers["Authorization"] = f"Bearer {access_token}"
                except Exception as exc:  # noqa: BLE001 - fail-open
                    _LOG.warning(
                        "mcp_tenant_injector: oauth token fetch failed for %r: %r",
                        name,
                        exc,
                    )
            else:
                _LOG.warning(
                    "mcp_tenant_injector: oauth auth missing server id for %r; "
                    "skipping token fetch",
                    name,
                )
        else:
            headers.update(_auth_to_headers(auth))
        http_kwargs: dict[str, Any] = {
            "url": url,
            "headers": headers or None,
        }
        if timeout_seconds is not None:
            http_kwargs["timeout"] = timeout_seconds
        config = HttpMCPConfig(**http_kwargs)
        return MCPClient(
            name=name,
            is_stateful=False,
            mcp_config=config,
            enable_tools=enable_tools,
            disable_tools=disable_tools,
            **exec_timeout_kw,
        )


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
