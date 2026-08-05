"""Minimal MCP client used by trusted, no-sandbox inline chat runs.

The sandbox workers use AgentScope / Claude SDK MCP clients.  Inline runs execute
inside the trusted session-orchestrator process, so they need a small transport
adapter that can:

* discover the tenant's enabled servers from mcp-registry;
* perform the Streamable HTTP initialize + tools/list handshake;
* expose MCP tools as OpenAI function-tool schemas; and
* execute tools/call and flatten the result for the existing inline tool loop.

Only HTTP servers are supported here.  ``stdio`` servers require a process and
filesystem boundary and therefore remain an agent/sandbox capability.
Every server is failure-isolated: a bad registry record, failed handshake or
tool error never removes healthy servers and never fails the surrounding chat.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MCP_PROTOCOL_VERSION = "2024-11-05"
_MAX_PAGES = 10
_MAX_TOOLS = 128
_MAX_RESULT_CHARS = 64_000
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


class InlineMcpError(Exception):
    """A registry, transport or JSON-RPC error isolated to one MCP server."""


def _json_rpc_payload(method: str, request_id: int, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


def _parse_sse_json(text: str, request_id: int) -> dict[str, Any]:
    """Return the JSON-RPC frame matching ``request_id`` from an SSE body."""
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:") :].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict) and frame.get("id") == request_id:
            return frame
    raise InlineMcpError("MCP SSE response did not contain the requested JSON-RPC frame")


def _flatten_result(result: dict[str, Any]) -> str:
    """Convert an MCP CallToolResult into bounded text for the model + audit UI."""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text") or ""))
            elif block_type == "image":
                mime = str(block.get("mimeType") or "image")
                parts.append(f"[MCP 返回图片：{mime}]")
            elif block_type == "audio":
                mime = str(block.get("mimeType") or "audio")
                parts.append(f"[MCP 返回音频：{mime}]")
            elif block_type == "resource":
                resource = block.get("resource")
                parts.append(json.dumps(resource, ensure_ascii=False, default=str))
            else:
                parts.append(json.dumps(block, ensure_ascii=False, default=str))

    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))

    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False, default=str))
    text = "\n".join(part for part in parts if part).strip() or "（MCP 工具未返回内容）"
    if result.get("isError"):
        text = "MCP 工具执行失败：" + text
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "\n…[结果已截断]"
    return text


def _safe_tool_name(server_name: str, remote_name: str, used: set[str]) -> str:
    """Build a stable OpenAI-compatible, collision-free MCP function name."""
    server = _TOOL_NAME_RE.sub("_", server_name).strip("_") or "server"
    remote = _TOOL_NAME_RE.sub("_", remote_name).strip("_") or "tool"
    base = f"mcp__{server}__{remote}"
    if len(base) > 64:
        digest = hashlib.sha256(base.encode()).hexdigest()[:8]
        base = f"{base[:55]}_{digest}"
    candidate = base
    suffix = 2
    while candidate in used:
        marker = f"_{suffix}"
        candidate = base[: 64 - len(marker)] + marker
        suffix += 1
    used.add(candidate)
    return candidate


class InlineMcpSession:
    """One initialized Streamable HTTP MCP server session."""

    def __init__(
        self,
        *,
        name: str,
        endpoint: str,
        timeout: float,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self._headers = dict(headers or {})
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._session_id = ""
        self._next_id = 1

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        try:
            response = await self._client.post(
                self.endpoint,
                json=_json_rpc_payload(method, request_id, params),
                headers=self._request_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise InlineMcpError(f"{self.name}: {method} transport failed: {exc}") from exc

        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id

        content_type = response.headers.get("content-type", "").lower()
        try:
            if "text/event-stream" in content_type:
                frame = _parse_sse_json(response.text, request_id)
            else:
                frame = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise InlineMcpError(f"{self.name}: {method} returned invalid JSON") from exc
        if not isinstance(frame, dict):
            raise InlineMcpError(f"{self.name}: {method} returned a non-object response")
        if frame.get("error"):
            err = frame["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise InlineMcpError(f"{self.name}: {method} failed: {message}")
        result = frame.get("result")
        if not isinstance(result, dict):
            raise InlineMcpError(f"{self.name}: {method} returned no result")
        return result

    async def _notify_initialized(self) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        try:
            response = await self._client.post(
                self.endpoint,
                json=payload,
                headers=self._request_headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise InlineMcpError(f"{self.name}: initialized notification failed: {exc}") from exc

    async def connect(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-platform-inline", "version": "0.1"},
            },
        )
        await self._notify_initialized()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(item for item in page if isinstance(item, dict))
            cursor_raw = result.get("nextCursor")
            cursor = str(cursor_raw) if cursor_raw else None
            if not cursor or len(tools) >= _MAX_TOOLS:
                break
        return tools[:_MAX_TOOLS]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        return _flatten_result(result)

    async def close(self) -> None:
        if self._session_id:
            try:
                await self._client.delete(self.endpoint, headers=self._request_headers())
            except httpx.HTTPError:
                logger.debug("inline MCP session teardown failed", extra={"server": self.name})
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True)
class InlineMcpTool:
    exposed_name: str
    remote_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any]
    session: InlineMcpSession = field(compare=False, repr=False)

    def openai_spec(self) -> dict[str, Any]:
        description = self.description.strip() or f"调用 {self.server_name} MCP 工具"
        return {
            "type": "function",
            "function": {
                "name": self.exposed_name,
                "description": f"[{self.server_name}] {description}"[:1024],
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


class InlineMcpCatalog:
    def __init__(
        self,
        tools: dict[str, InlineMcpTool] | None = None,
        sessions: list[InlineMcpSession] | None = None,
    ) -> None:
        self._tools = tools or {}
        self._sessions = sessions or []

    @property
    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool.openai_spec() for tool in self._tools.values()]

    def has(self, exposed_name: str) -> bool:
        return exposed_name in self._tools

    async def call(self, exposed_name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(exposed_name)
        if tool is None:
            raise InlineMcpError(f"unknown inline MCP tool: {exposed_name}")
        return await tool.session.call_tool(tool.remote_name, arguments)

    async def close(self) -> None:
        await asyncio_gather_safely(*(session.close() for session in self._sessions))


async def asyncio_gather_safely(*awaitables: Any) -> None:
    """Gather teardown coroutines without allowing one server to block others."""
    if not awaitables:
        return
    import asyncio

    await asyncio.gather(*awaitables, return_exceptions=True)


async def _fetch_registry_servers(
    *,
    base_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    timeout: float,
    server_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return the tenant's enabled MCP server records, optionally narrowed.

    ``server_ids`` carries the run's presence-resolved selection:

    * ``None``      — the caller expressed no choice: return every
      enabled-by-default server (the historical behaviour).
    * ``[]``        — the caller explicitly disabled MCP for this run: return
      nothing, and skip the registry round-trip entirely.
    * non-empty     — return only the enabled servers whose ``id`` is listed.

    The ``[]`` case is checked with ``is not None`` rather than truthiness on
    purpose: an empty selection is a real instruction, not a missing one.
    """
    if not base_url:
        return []
    if server_ids is not None and not server_ids:
        return []
    headers = {
        "X-Tenant-Id": tenant_id,
        "X-User-Id": user_id or "SYSTEM",
        "X-Actor": "SYSTEM",
        "X-Service-Scopes": service_scopes,
        "Accept": "application/json",
    }
    url = f"{base_url.rstrip('/')}/internal/mcp-servers"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, params={"tenant_id": tenant_id}, headers=headers)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("inline MCP registry discovery failed (fail-open): %s", exc)
        return []
    items = body.get("items", []) if isinstance(body, dict) else body
    if not isinstance(items, list):
        return []
    enabled = [
        item for item in items if isinstance(item, dict) and item.get("enabled", True)
    ]
    if server_ids is None:
        return enabled
    wanted = {str(sid) for sid in server_ids}
    return [item for item in enabled if str(item.get("id") or "") in wanted]


def _auth_to_headers(auth: Any) -> dict[str, str]:
    """Convert a registry ``auth`` record into HTTP headers for the upstream.

    Handles the I/O-free auth types (``bearer`` / ``api_key``).  ``oauth`` is
    handled separately by :func:`_build_inline_headers` because it requires an
    async token fetch.

    ``auth`` shape: ``{type: "none"|"bearer"|"api_key", token?, header_name?,
    api_key?}``. ``header_name`` defaults to ``X-API-Key`` for ``api_key``.
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
# OAuth client-credentials token cache (inline MCP servers)
# ---------------------------------------------------------------------------
#
# Inline runs connect DIRECTLY to the upstream (no broker), so the orchestrator
# injects upstream auth itself.  For ``auth.type == "oauth"`` it performs the
# client-credentials flow (POST to the registry-supplied ``token_url``), caches
# the access_token keyed by ``server_id``, and injects
# ``Authorization: Bearer {access_token}``.
#
# Cache: module-level ``{server_id: {access_token, expires_at}}``.  A token is
# refreshed when within ``_OAUTH_REFRESH_MARGIN_SECONDS`` of expiry.  Single-
# flight: a per-``server_id`` ``asyncio.Lock`` ensures concurrent inline runs
# for the same server share one token fetch (no duplicate POSTs).
_OAUTH_TOKEN_CACHE: dict[str, dict[str, object]] = {}
_OAUTH_LOCKS: dict[str, asyncio.Lock] = {}
_OAUTH_REFRESH_MARGIN_SECONDS = 60
_OAUTH_TOKEN_TIMEOUT_SECONDS = 10.0


def _oauth_lock(server_id: str) -> asyncio.Lock:
    """Return the per-``server_id`` single-flight lock, creating it lazily."""
    lock = _OAUTH_LOCKS.get(server_id)
    if lock is None:
        lock = asyncio.Lock()
        _OAUTH_LOCKS[server_id] = lock
    return lock


async def _fetch_oauth_token(auth: Any) -> tuple[str, float]:
    """POST ``client_credentials`` to ``auth.token_url`` -> (access_token, expires_at).

    ``expires_at`` is a unix-epoch seconds timestamp.  ``expires_in`` defaults to
    3600s when the token endpoint omits it or returns a non-numeric value.
    Raises on any transport / HTTP / shape error so the caller can fail-open.
    """
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
    async with httpx.AsyncClient(timeout=_OAUTH_TOKEN_TIMEOUT_SECONDS) as client:
        resp = await client.post(token_url, data=form)
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


async def _get_oauth_token(server_id: str, auth: Any) -> str:
    """Return a cached oauth access_token for ``server_id``, refreshing on expiry.

    Single-flight: concurrent callers for the same ``server_id`` share one fetch
    via a per-``server_id`` ``asyncio.Lock`` so the token endpoint is hit once.
    """
    async with _oauth_lock(server_id):
        cached = _OAUTH_TOKEN_CACHE.get(server_id)
        now = time.time()
        if (
            cached
            and float(cached.get("expires_at", 0.0)) - now
            > _OAUTH_REFRESH_MARGIN_SECONDS
        ):
            return str(cached["access_token"])
        access_token, expires_at = await _fetch_oauth_token(auth)
        _OAUTH_TOKEN_CACHE[server_id] = {
            "access_token": access_token,
            "expires_at": expires_at,
        }
        return access_token


async def _build_inline_headers(server: dict[str, Any]) -> dict[str, str]:
    """Merge a server's custom ``headers`` + ``auth`` into the upstream header set.

    Inline runs connect DIRECTLY to the upstream (no broker), so the
    orchestrator injects upstream auth itself - unlike broker-routed workers,
    which leave this to the tool-broker.  For ``oauth`` the orchestrator
    performs the client-credentials flow (cached + single-flight by server_id)
    and injects ``Authorization: Bearer {access_token}``; a fetch failure is
    fail-open (logged + no Authorization header) so the MCP handshake surfaces
    the upstream's own 401 and the server is skipped (failure-isolated).  Auth
    headers win over colliding custom ``headers`` keys.
    """
    headers: dict[str, str] = {}
    custom = server.get("headers")
    if isinstance(custom, dict):
        headers.update({str(k): str(v) for k, v in custom.items()})
    auth = server.get("auth")
    if isinstance(auth, dict):
        auth_type = str(auth.get("type") or "none").lower()
        if auth_type == "oauth":
            server_id = str(server.get("id") or "").strip()
            if server_id:
                try:
                    access_token = await _get_oauth_token(server_id, auth)
                    headers["Authorization"] = f"Bearer {access_token}"
                except Exception as exc:  # noqa: BLE001 - fail-open
                    logger.warning(
                        "inline MCP oauth token fetch failed for %s: %s",
                        server_id,
                        exc,
                    )
            else:
                logger.warning(
                    "inline MCP oauth auth missing server id; skipping token fetch"
                )
        else:
            headers.update(_auth_to_headers(auth))
    return headers


def _resolve_inline_timeout(server: dict[str, Any], default: float) -> float:
    """Per-server ``timeout_seconds`` when valid, else the catalog default."""
    raw = server.get("timeout_seconds")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return default


def _filter_inline_tools(
    raw_tools: list[dict[str, Any]], tool_filter: Any
) -> list[dict[str, Any]]:
    """Apply a registry ``tool_filter`` {allow, block} to a tools/list result.

    allow non-empty -> keep only allow; block non-empty -> drop block. allow is
    applied first (whitelist) then block, matching the other consumers.
    """
    if not isinstance(tool_filter, dict):
        return raw_tools
    allow_raw = (
        tool_filter.get("allow") if isinstance(tool_filter.get("allow"), list) else []
    )
    block_raw = (
        tool_filter.get("block") if isinstance(tool_filter.get("block"), list) else []
    )
    allow = {str(t) for t in allow_raw if str(t)}
    block = {str(t) for t in block_raw if str(t)}
    if not allow and not block:
        return raw_tools
    out = raw_tools
    if allow:
        out = [t for t in out if str(t.get("name") or "") in allow]
    if block:
        out = [t for t in out if str(t.get("name") or "") not in block]
    return out


async def load_inline_mcp_catalog(
    *,
    base_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    timeout: float,
    server_ids: list[str] | None = None,
) -> InlineMcpCatalog:
    """Discover and initialize the run's healthy tenant HTTP MCP servers.

    ``server_ids`` is the run's presence-resolved MCP selection: ``None`` =
    every enabled-by-default tenant server, ``[]`` = none at all for this run,
    non-empty = only those ids.  See :func:`_fetch_registry_servers`.
    """
    servers = await _fetch_registry_servers(
        base_url=base_url,
        service_scopes=service_scopes,
        tenant_id=tenant_id,
        user_id=user_id,
        timeout=timeout,
        server_ids=server_ids,
    )
    tools: dict[str, InlineMcpTool] = {}
    sessions: list[InlineMcpSession] = []
    used_names: set[str] = set()

    for server in servers:
        transport = str(server.get("transport") or "").lower()
        endpoint = str(server.get("endpoint") or "").strip()
        server_name = str(server.get("name") or "MCP").strip() or "MCP"
        if transport == "sse":
            # Legacy SSE (dual-endpoint GET /sse + POST) is not supported by the
            # inline Streamable HTTP client. Only the SDK worker direct path
            # supports it natively; skip here so the inline run stays healthy.
            logger.warning(
                "inline MCP skipping SSE server (legacy SSE not supported in "
                "inline mode; use worker direct mode)",
                extra={"server": server_name, "transport": transport},
            )
            continue
        if transport != "http" or not endpoint:
            logger.info(
                "inline MCP skipping non-HTTP server",
                extra={"server": server_name, "transport": transport},
            )
            continue

        # Inline runs connect DIRECTLY to the upstream (no broker), so the
        # orchestrator injects the registry-supplied upstream auth + custom
        # headers itself (broker-routed workers leave this to the tool-broker).
        # For ``auth.type == "oauth"`` the orchestrator fetches a client-
        # credentials token (cached + single-flight by server_id) and injects
        # the bearer here too.
        headers = await _build_inline_headers(server)
        # Per-server timeout_seconds overrides the catalog-wide default.
        session_timeout = _resolve_inline_timeout(server, timeout)
        session = InlineMcpSession(
            name=server_name,
            endpoint=endpoint,
            timeout=session_timeout,
            headers=headers,
        )
        try:
            await session.connect()
            raw_tools = await session.list_tools()
        except Exception as exc:  # noqa: BLE001 — isolate one broken server
            logger.warning(
                "inline MCP server unavailable (skipping): %s",
                exc,
                extra={"server": server_name},
            )
            await session.close()
            continue

        # tool_filter: allow non-empty -> keep only allow; block non-empty ->
        # drop block. Applied AFTER list_tools as an exposure filter so the
        # model never sees filtered tools. allow first (whitelist), then block.
        raw_tools = _filter_inline_tools(raw_tools, server.get("tool_filter"))

        sessions.append(session)
        for raw in raw_tools:
            remote_name = str(raw.get("name") or "").strip()
            if not remote_name:
                continue
            exposed_name = _safe_tool_name(server_name, remote_name, used_names)
            schema = raw.get("inputSchema") or raw.get("input_schema")
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            tools[exposed_name] = InlineMcpTool(
                exposed_name=exposed_name,
                remote_name=remote_name,
                server_name=server_name,
                description=str(raw.get("description") or ""),
                input_schema=schema,
                session=session,
            )

    if tools:
        logger.info(
            "inline MCP loaded tools",
            extra={"tenant_id": tenant_id, "servers": len(sessions), "tools": len(tools)},
        )
    return InlineMcpCatalog(tools, sessions)


__all__ = [
    "InlineMcpCatalog",
    "InlineMcpError",
    "InlineMcpSession",
    "InlineMcpTool",
    "load_inline_mcp_catalog",
]
