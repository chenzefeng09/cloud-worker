from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_session_orchestrator.services import inline_mcp_client as mcp
from agent_session_orchestrator.services.inline_mcp_client import InlineMcpSession


@pytest.mark.asyncio
async def test_streamable_http_handshake_lists_and_calls_tool() -> None:
    requests: list[tuple[str, dict[str, Any], str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            requests.append(("DELETE", {}, request.headers.get("Mcp-Session-Id", "")))
            return httpx.Response(204, request=request)

        body = json.loads(request.content)
        method = str(body.get("method") or "")
        requests.append((method, body, request.headers.get("Mcp-Session-Id", "")))
        if method == "initialize":
            return httpx.Response(
                200,
                request=request,
                headers={"Mcp-Session-Id": "mcp-session-1"},
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "echo", "version": "1"},
                    },
                },
            )
        if method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if method == "tools/list":
            return httpx.Response(
                200,
                request=request,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo input text",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }
                        ]
                    },
                },
            )
        if method == "tools/call":
            assert body["params"] == {"name": "echo", "arguments": {"text": "你好"}}
            return httpx.Response(
                200,
                request=request,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"content": [{"type": "text", "text": "ECHO: 你好"}]},
                },
            )
        raise AssertionError(f"unexpected MCP request: {method}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = InlineMcpSession(
        name="echo",
        endpoint="https://mcp.example/mcp",
        timeout=5,
        client=client,
    )
    try:
        await session.connect()
        tools = await session.list_tools()
        result = await session.call_tool("echo", {"text": "你好"})
    finally:
        await session.close()
        await client.aclose()

    assert tools[0]["name"] == "echo"
    assert result == "ECHO: 你好"
    assert [item[0] for item in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
        "DELETE",
    ]
    # The session id returned by initialize is attached to every later request.
    assert all(item[2] == "mcp-session-1" for item in requests[1:])


@pytest.mark.asyncio
async def test_sse_json_rpc_response_is_supported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body.get("method")
        if method == "notifications/initialized":
            return httpx.Response(202, request=request)
        frame = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": (
                {"protocolVersion": "2024-11-05", "capabilities": {}}
                if method == "initialize"
                else {"tools": []}
            ),
        }
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=f"event: message\ndata: {json.dumps(frame)}\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = InlineMcpSession(
            name="sse-server",
            endpoint="https://mcp.example/mcp",
            timeout=5,
            client=client,
        )
        await session.connect()
        assert await session.list_tools() == []


@pytest.mark.asyncio
async def test_catalog_skips_broken_and_stdio_servers(monkeypatch) -> None:
    servers = [
        {"name": "echo service", "transport": "http", "endpoint": "https://echo/mcp"},
        {"name": "broken", "transport": "http", "endpoint": "https://broken/mcp"},
        {"name": "local", "transport": "stdio", "command": "local-mcp"},
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    created: list[Any] = []

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            self.name = name
            self.closed = False
            created.append(self)

        async def connect(self):
            if self.name == "broken":
                raise mcp.InlineMcpError("unreachable")

        async def list_tools(self):
            return [{
                "name": "say hello",
                "description": "Say hello",
                "inputSchema": {"type": "object", "properties": {}},
            }]

        async def call_tool(self, name, arguments):
            return f"called {name}"

        async def close(self):
            self.closed = True

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    catalog = await mcp.load_inline_mcp_catalog(
        base_url="http://registry/api/mcp/v1",
        service_scopes="mcp.read_internal",
        tenant_id="tnt_1",
        user_id="usr_1",
        timeout=5,
    )
    try:
        assert len(catalog.openai_tools) == 1
        exposed = catalog.openai_tools[0]["function"]["name"]
        assert exposed == "mcp__echo_service__say_hello"
        assert await catalog.call(exposed, {}) == "called say hello"
    finally:
        await catalog.close()

    # Broken HTTP server is closed immediately; healthy one closes with catalog.
    assert len(created) == 2
    assert all(session.closed for session in created)
