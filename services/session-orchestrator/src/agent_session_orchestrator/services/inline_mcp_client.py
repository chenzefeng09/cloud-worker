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

import hashlib
import json
import logging
import re
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
    *, base_url: str, service_scopes: str, tenant_id: str, user_id: str, timeout: float
) -> list[dict[str, Any]]:
    if not base_url:
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
    return [item for item in items if isinstance(item, dict) and item.get("enabled", True)]


async def load_inline_mcp_catalog(
    *,
    base_url: str,
    service_scopes: str,
    tenant_id: str,
    user_id: str,
    timeout: float,
) -> InlineMcpCatalog:
    """Discover and initialize all healthy tenant HTTP MCP servers."""
    servers = await _fetch_registry_servers(
        base_url=base_url,
        service_scopes=service_scopes,
        tenant_id=tenant_id,
        user_id=user_id,
        timeout=timeout,
    )
    tools: dict[str, InlineMcpTool] = {}
    sessions: list[InlineMcpSession] = []
    used_names: set[str] = set()

    for server in servers:
        transport = str(server.get("transport") or "").lower()
        endpoint = str(server.get("endpoint") or "").strip()
        server_name = str(server.get("name") or "MCP").strip() or "MCP"
        if transport != "http" or not endpoint:
            logger.info(
                "inline MCP skipping non-HTTP server",
                extra={"server": server_name, "transport": transport},
            )
            continue

        manifest_headers = server.get("headers")
        headers = (
            {str(k): str(v) for k, v in manifest_headers.items()}
            if isinstance(manifest_headers, dict)
            else {}
        )
        session = InlineMcpSession(
            name=server_name,
            endpoint=endpoint,
            timeout=timeout,
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
