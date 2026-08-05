from __future__ import annotations

import asyncio
import json
import time
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


@pytest.mark.asyncio
async def test_catalog_skips_sse_servers(monkeypatch, caplog) -> None:
    """Legacy SSE servers are not supported by the inline Streamable HTTP
    client; they are skipped with a warning (only the SDK worker direct path
    supports SSE natively). A healthy http server alongside is still loaded."""
    servers = [
        {"name": "legacy-sse", "transport": "sse", "endpoint": "https://legacy/sse"},
        {"name": "echo", "transport": "http", "endpoint": "https://echo/mcp"},
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    created: list[Any] = []

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            self.name = name
            created.append(self)

        async def connect(self):
            pass

        async def list_tools(self):
            return [
                {
                    "name": "say hello",
                    "description": "Say hello",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        async def call_tool(self, name, arguments):
            return f"called {name}"

        async def close(self):
            pass

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    with caplog.at_level("WARNING"):
        catalog = await mcp.load_inline_mcp_catalog(
            base_url="http://registry/api/mcp/v1",
            service_scopes="mcp.read_internal",
            tenant_id="tnt_1",
            user_id="usr_1",
            timeout=5,
        )
    try:
        # Only the http server was connected; the sse server was skipped.
        assert len(created) == 1
        assert created[0].name == "echo"
        assert len(catalog.openai_tools) == 1
        sse_warnings = [
            r for r in caplog.records
            if "skipping SSE server" in r.message
        ]
        assert sse_warnings, "expected an SSE-skip warning"
        # The server name is carried in the structured extra, not the message.
        assert getattr(sse_warnings[0], "server", None) == "legacy-sse"
        assert getattr(sse_warnings[0], "transport", None) == "sse"
    finally:
        await catalog.close()


@pytest.mark.asyncio
async def test_catalog_injects_auth_headers_and_per_server_timeout(monkeypatch) -> None:
    """Inline runs connect directly, so the orchestrator injects registry auth +
    custom headers and applies per-server timeout_seconds (overriding default)."""
    servers = [
        {
            "name": "echo",
            "transport": "http",
            "endpoint": "https://echo/mcp",
            "headers": {"X-Custom": "reg"},
            "auth": {"type": "bearer", "token": "upstream-tok"},
            "timeout_seconds": 42,
        }
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    created: list[Any] = []

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            self.name = name
            self.timeout = timeout
            self.headers = headers
            created.append(self)

        async def connect(self):
            pass

        async def list_tools(self):
            return [
                {
                    "name": "echo",
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        async def call_tool(self, name, arguments):
            return "ok"

        async def close(self):
            pass

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    catalog = await mcp.load_inline_mcp_catalog(
        base_url="http://registry",
        service_scopes="x",
        tenant_id="t",
        user_id="u",
        timeout=5,
    )
    await catalog.close()

    assert len(created) == 1
    s = created[0]
    # Per-server timeout overrides the catalog default (5).
    assert s.timeout == 42
    # Custom headers + bearer auth merged (auth wins on collision).
    assert s.headers == {"X-Custom": "reg", "Authorization": "Bearer upstream-tok"}


@pytest.mark.asyncio
async def test_catalog_applies_tool_filter(monkeypatch) -> None:
    """tool_filter {allow, block}: allow whitelists, then block removes."""
    servers = [
        {
            "name": "echo",
            "transport": "http",
            "endpoint": "https://echo/mcp",
            "tool_filter": {"allow": ["echo", "ping"], "block": ["secret"]},
        }
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            pass

        async def connect(self):
            pass

        async def list_tools(self):
            return [
                {"name": "echo", "description": "e", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "ping", "description": "p", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "secret", "description": "s", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "other", "description": "o", "inputSchema": {"type": "object", "properties": {}}},
            ]

        async def call_tool(self, name, arguments):
            return "ok"

        async def close(self):
            pass

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    catalog = await mcp.load_inline_mcp_catalog(
        base_url="http://registry",
        service_scopes="x",
        tenant_id="t",
        user_id="u",
        timeout=5,
    )
    try:
        # allow keeps echo+ping; block removes secret; "other" not in allow -> dropped.
        assert catalog.has("mcp__echo__echo")
        assert catalog.has("mcp__echo__ping")
        assert not catalog.has("mcp__echo__secret")
        assert not catalog.has("mcp__echo__other")
    finally:
        await catalog.close()


# ---------------------------------------------------------------------------
# OAuth client-credentials auth (inline MCP servers)
# ---------------------------------------------------------------------------
#
# Inline runs connect DIRECTLY to the upstream (no broker), so the orchestrator
# performs the client-credentials flow itself and injects the bearer.  These
# tests stub ``_fetch_oauth_token`` to avoid real HTTP for the cache / single-
# flight / integration logic, plus one direct test of the POST body shape.


def _reset_inline_oauth_cache() -> None:
    mcp._OAUTH_TOKEN_CACHE.clear()
    mcp._OAUTH_LOCKS.clear()


@pytest.mark.asyncio
async def test_catalog_oauth_fetches_token_and_injects_bearer(monkeypatch) -> None:
    """A server with ``auth.type == oauth`` gets its bearer injected into the
    session headers (inline fetches the token itself - no broker)."""
    _reset_inline_oauth_cache()
    fetch_calls = 0

    async def fake_fetch(auth: Any) -> tuple[str, float]:
        nonlocal fetch_calls
        fetch_calls += 1
        return "tok-xyz", time.time() + 3600

    monkeypatch.setattr(mcp, "_fetch_oauth_token", fake_fetch)

    servers = [
        {
            "id": "mcp_oauth1",
            "name": "echo",
            "transport": "http",
            "endpoint": "https://echo/mcp",
            "auth": {
                "type": "oauth",
                "client_id": "c",
                "client_secret": "s",
                "token_url": "https://idp.example/token",
                "scopes": ["a", "b"],
            },
        }
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    created: list[Any] = []

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            self.name = name
            self.headers = headers
            created.append(self)

        async def connect(self):
            pass

        async def list_tools(self):
            return [
                {
                    "name": "echo",
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        async def call_tool(self, name, arguments):
            return "ok"

        async def close(self):
            pass

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    catalog = await mcp.load_inline_mcp_catalog(
        base_url="http://registry",
        service_scopes="x",
        tenant_id="t",
        user_id="u",
        timeout=5,
    )
    await catalog.close()

    assert fetch_calls == 1
    assert created[0].headers == {"Authorization": "Bearer tok-xyz"}


@pytest.mark.asyncio
async def test_catalog_oauth_fetch_failure_is_fail_open(monkeypatch, caplog) -> None:
    """A token-endpoint failure is fail-open: no Authorization header, server
    still registered (the MCP handshake surfaces the upstream's own 401)."""
    _reset_inline_oauth_cache()

    async def boom(auth: Any) -> tuple[str, float]:
        raise RuntimeError("idp unreachable")

    monkeypatch.setattr(mcp, "_fetch_oauth_token", boom)

    servers = [
        {
            "id": "mcp_oauth_fail",
            "name": "echo",
            "transport": "http",
            "endpoint": "https://echo/mcp",
            "auth": {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"},
        }
    ]
    monkeypatch.setattr(mcp, "_fetch_registry_servers", AsyncMock(return_value=servers))

    created: list[Any] = []

    class FakeSession:
        def __init__(self, *, name, endpoint, timeout, headers):
            self.headers = headers
            created.append(self)

        async def connect(self):
            pass

        async def list_tools(self):
            return [
                {
                    "name": "echo",
                    "description": "",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

        async def call_tool(self, name, arguments):
            return "ok"

        async def close(self):
            pass

    monkeypatch.setattr(mcp, "InlineMcpSession", FakeSession)

    with caplog.at_level("WARNING"):
        catalog = await mcp.load_inline_mcp_catalog(
            base_url="http://registry",
            service_scopes="x",
            tenant_id="t",
            user_id="u",
            timeout=5,
        )
    await catalog.close()

    # No Authorization header was injected (fail-open).
    assert created[0].headers == {}
    assert any("oauth token fetch failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_oauth_fetch_posts_client_credentials_form(monkeypatch) -> None:
    """_fetch_oauth_token POSTs the client_credentials form with space-joined
    scopes and parses {access_token, expires_in}."""
    captured: dict[str, Any] = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"access_token": "tok", "expires_in": 7200}

    class _FakeClient:
        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> _FakeResp:
            captured["url"] = url
            captured["data"] = data
            return _FakeResp()

    monkeypatch.setattr(mcp.httpx, "AsyncClient", lambda **_kw: _FakeClient())

    auth = {
        "type": "oauth",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://idp.example/token",
        "scopes": ["a", "b"],
    }
    token, expires_at = await mcp._fetch_oauth_token(auth)
    assert token == "tok"
    assert captured["url"] == "https://idp.example/token"
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["client_id"] == "cid"
    assert captured["data"]["client_secret"] == "csec"
    assert captured["data"]["scope"] == "a b"
    # expires_in=7200 -> expires_at ~ now + 7200.
    assert expires_at > time.time() + 7000


@pytest.mark.asyncio
async def test_oauth_get_token_single_flight(monkeypatch) -> None:
    """Concurrent _get_oauth_token calls for one server_id share ONE fetch."""
    _reset_inline_oauth_cache()
    calls = 0

    async def fake_fetch(auth: Any) -> tuple[str, float]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "tok", time.time() + 3600

    monkeypatch.setattr(mcp, "_fetch_oauth_token", fake_fetch)
    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    results = await asyncio.gather(
        mcp._get_oauth_token("srv_sf", auth),
        mcp._get_oauth_token("srv_sf", auth),
        mcp._get_oauth_token("srv_sf", auth),
    )
    assert calls == 1
    assert results == ["tok", "tok", "tok"]


@pytest.mark.asyncio
async def test_oauth_get_token_caches_valid_token(monkeypatch) -> None:
    """A cached token within its refresh margin is reused without refetch."""
    _reset_inline_oauth_cache()
    calls = 0

    async def fake_fetch(auth: Any) -> tuple[str, float]:
        nonlocal calls
        calls += 1
        return f"tok-{calls}", time.time() + 3600

    monkeypatch.setattr(mcp, "_fetch_oauth_token", fake_fetch)
    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    first = await mcp._get_oauth_token("srv_cache", auth)
    second = await mcp._get_oauth_token("srv_cache", auth)
    assert calls == 1
    assert first == second == "tok-1"


@pytest.mark.asyncio
async def test_oauth_get_token_refreshes_after_expiry(monkeypatch) -> None:
    """A cached token past its refresh margin is refetched."""
    _reset_inline_oauth_cache()
    calls = 0

    async def fake_fetch(auth: Any) -> tuple[str, float]:
        nonlocal calls
        calls += 1
        return f"tok-{calls}", time.time() - 1  # already expired

    monkeypatch.setattr(mcp, "_fetch_oauth_token", fake_fetch)
    auth = {"type": "oauth", "client_id": "c", "client_secret": "s", "token_url": "u"}

    first = await mcp._get_oauth_token("srv_refresh", auth)
    second = await mcp._get_oauth_token("srv_refresh", auth)
    assert calls == 2
    assert first == "tok-1"
    assert second == "tok-2"


# ─────────────────────────────────────────────────────────────────────────────
# mcp_server_ids: per-run selection with presence semantics
# ─────────────────────────────────────────────────────────────────────────────


def _registry_transport(items: list[dict[str, Any]], seen: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, request=request, json={"items": items})

    return httpx.MockTransport(handler)


_REGISTRY_ITEMS = [
    {"id": "srv_a", "name": "A", "transport": "http", "endpoint": "http://a/mcp"},
    {"id": "srv_b", "name": "B", "transport": "http", "endpoint": "http://b/mcp"},
    {
        "id": "srv_off",
        "name": "Off",
        "transport": "http",
        "endpoint": "http://off/mcp",
        "enabled": False,
    },
]


async def _fetch_with(server_ids, monkeypatch, seen: list[httpx.Request]):
    transport = _registry_transport(_REGISTRY_ITEMS, seen)
    real_client = httpx.AsyncClient

    def _factory(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw.pop("transport", None)
        return real_client(*a, transport=transport, **kw)

    monkeypatch.setattr(mcp.httpx, "AsyncClient", _factory)
    return await mcp._fetch_registry_servers(
        base_url="http://registry",
        service_scopes="s",
        tenant_id="ten_1",
        user_id="usr_1",
        timeout=5.0,
        server_ids=server_ids,
    )


@pytest.mark.asyncio
async def test_fetch_registry_servers_none_returns_all_enabled(monkeypatch) -> None:
    """``None`` = caller expressed no choice → every enabled tenant server."""
    seen: list[httpx.Request] = []
    servers = await _fetch_with(None, monkeypatch, seen)
    assert [s["id"] for s in servers] == ["srv_a", "srv_b"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_fetch_registry_servers_empty_list_returns_nothing(monkeypatch) -> None:
    """``[]`` = explicitly disable all MCP; must NOT fall back to the default set.

    A truthiness check here would turn "disable all" back into "inject all".
    The registry round-trip is also skipped — there is nothing to select from.
    """
    seen: list[httpx.Request] = []
    servers = await _fetch_with([], monkeypatch, seen)
    assert servers == []
    assert seen == [], "registry must not be queried when MCP is disabled"


@pytest.mark.asyncio
async def test_fetch_registry_servers_narrows_to_selected_ids(monkeypatch) -> None:
    """A non-empty selection keeps only those ids, and only if still enabled."""
    seen: list[httpx.Request] = []
    servers = await _fetch_with(["srv_b", "srv_off", "srv_missing"], monkeypatch, seen)
    assert [s["id"] for s in servers] == ["srv_b"]


@pytest.mark.asyncio
async def test_load_inline_mcp_catalog_forwards_server_ids(monkeypatch) -> None:
    """``load_inline_mcp_catalog`` relays the selection to the registry fetch."""
    captured: dict[str, Any] = {}

    async def _fake_fetch(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mcp, "_fetch_registry_servers", _fake_fetch)
    catalog = await mcp.load_inline_mcp_catalog(
        base_url="http://registry",
        service_scopes="s",
        tenant_id="ten_1",
        user_id="usr_1",
        timeout=5.0,
        server_ids=[],
    )
    assert captured["server_ids"] == []
    assert catalog.openai_tools == []


@pytest.mark.asyncio
async def test_load_inline_mcp_catalog_defaults_server_ids_to_none(monkeypatch) -> None:
    """Omitting the kwarg keeps the pre-feature behaviour (no narrowing)."""
    captured: dict[str, Any] = {}

    async def _fake_fetch(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(mcp, "_fetch_registry_servers", _fake_fetch)
    await mcp.load_inline_mcp_catalog(
        base_url="http://registry",
        service_scopes="s",
        tenant_id="ten_1",
        user_id="usr_1",
        timeout=5.0,
    )
    assert captured["server_ids"] is None
