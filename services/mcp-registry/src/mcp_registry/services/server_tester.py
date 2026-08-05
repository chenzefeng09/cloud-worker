"""Test-connection helper for MCP servers.

Performs a real Streamable HTTP JSON-RPC handshake against an MCP server:
``initialize`` -> ``notifications/initialized`` -> ``tools/list``.  Mirrors the
transport pattern in ``session-orchestrator.services.inline_mcp_client`` but is
self-contained so mcp-registry does not depend on the orchestrator package.

Auth handling:

  * ``bearer`` / ``api_key`` - static credentials injected per-request.
  * ``oauth`` - a one-shot client-credentials flow is run against
    ``auth.token_url`` to obtain an ``access_token`` (test connections never
    cache tokens); the resulting bearer token is injected on every handshake
    request.  A token-fetch failure short-circuits the test with ``ok=false``.

The function never raises - every failure path (transport error, JSON-RPC
error, malformed response, oauth error) is translated into
``McpTestResult(ok=False, ...)``.  ``stdio`` and ``sse`` servers are rejected
by :func:`test_connection` (the transport dispatcher) before the HTTP
handshake runs - ``stdio`` needs a local process and ``sse`` is the legacy
dual-endpoint transport that only the SDK worker path supports natively.

The ``server`` argument only needs ``endpoint`` / ``headers`` / ``auth`` /
``timeout_seconds`` attributes; the API layer passes a lightweight view so the
ORM row is not mutated (decrypting auth on the ORM object would flush
plaintext back to the DB).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, Protocol

import httpx

from mcp_registry.schemas.mcp_server import McpTestResult, McpTestTool

_MCP_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT_SECONDS = 10


class _TestableServer(Protocol):
    """Structural shape needed from a server for the handshake."""

    transport: str
    endpoint: str | None
    headers: dict[str, str]
    auth: dict[str, Any] | None
    timeout_seconds: int | None


class _McpTestError(Exception):
    """JSON-RPC or transport error raised inside the handshake pipeline."""


def _build_request_headers(server: _TestableServer) -> dict[str, str]:
    """Merge custom headers with static auth-derived headers.

    Custom headers are applied first; auth headers are applied on top so the
    structured auth config is authoritative when the two collide.  OAuth is
    handled separately in ``test_http_connection`` because it needs an async
    token fetch.
    """
    headers: dict[str, str] = {}

    custom = server.headers
    if isinstance(custom, dict):
        for key, value in custom.items():
            headers[str(key)] = str(value)

    auth = server.auth
    if isinstance(auth, dict):
        auth_type = str(auth.get("type") or "none").lower()
        if auth_type == "bearer":
            token = auth.get("token")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key":
            api_key = auth.get("api_key")
            if api_key:
                header_name = str(auth.get("header_name") or "X-API-Key") or "X-API-Key"
                headers[header_name] = str(api_key)
    return headers


async def _fetch_oauth_access_token(
    auth: dict[str, Any],
    timeout_seconds: int,
) -> str | None:
    """Run a one-shot client-credentials flow and return the access token.

    Returns ``None`` on any failure (transport error, non-200, missing
    ``access_token``); the caller surfaces this as a test failure.
    """
    token_url = (auth.get("token_url") or "").strip()
    client_id = auth.get("client_id")
    client_secret = auth.get("client_secret")
    if not token_url or not client_id or not client_secret:
        return None

    scopes = auth.get("scopes")
    form: dict[str, str] = {
        "grant_type": "client_credentials",
        "client_id": str(client_id),
        "client_secret": str(client_secret),
    }
    if isinstance(scopes, list) and scopes:
        form["scope"] = " ".join(str(s) for s in scopes if s)

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                token_url,
                data=form,
                headers={"Accept": "application/json"},
            )
        if resp.status_code != 200:
            return None
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        return None

    if not isinstance(body, dict):
        return None
    token = body.get("access_token")
    return str(token) if token else None


def _parse_response(response: httpx.Response, request_id: int) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
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
        raise _McpTestError("SSE response did not contain the requested JSON-RPC frame")
    try:
        frame = response.json()
    except ValueError as exc:
        raise _McpTestError("response was not valid JSON") from exc
    if not isinstance(frame, dict):
        raise _McpTestError("response was not a JSON object")
    return frame


def _raise_for_rpc_error(frame: dict[str, Any]) -> None:
    err = frame.get("error")
    if isinstance(err, dict):
        message = err.get("message") or str(err)
        raise _McpTestError(f"JSON-RPC error: {message}")


async def test_connection(server: _TestableServer) -> McpTestResult:
    """Dispatch a test connection based on the server's transport.

    ``sse`` servers use the legacy dual-endpoint SSE transport (``GET /sse`` +
    ``POST``) which the registry tester does not implement - only the SDK
    worker path supports it natively.  ``stdio`` servers need a local process
    the tester cannot spawn.  Both return a deterministic not-supported result
    so the endpoint never raises.  ``http`` runs the full Streamable HTTP
    handshake via :func:`test_http_connection`.
    """
    transport = str(getattr(server, "transport", "") or "").lower()
    if transport == "sse":
        return McpTestResult(
            ok=False,
            server_info=None,
            tools=[],
            error=(
                "sse test connection not supported via registry tester; "
                "SDK worker path supports it natively"
            ),
        )
    if transport != "http":
        # stdio (or any unknown transport) is not reachable from this process.
        return McpTestResult(
            ok=False,
            server_info=None,
            tools=[],
            error="stdio test connection not supported yet",
        )
    return await test_http_connection(server)


async def test_http_connection(server: _TestableServer) -> McpTestResult:
    """Run the initialize + tools/list handshake and return a bounded result."""
    endpoint = (server.endpoint or "").strip()
    if not endpoint:
        return McpTestResult(ok=False, error="endpoint is not configured")

    timeout_seconds = server.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
    base_headers = _build_request_headers(server)

    # OAuth: obtain a one-shot access token before the handshake.  A token
    # fetch failure fails the test outright - there is no static credential
    # to fall back on.
    auth = server.auth
    if isinstance(auth, dict) and str(auth.get("type") or "none").lower() == "oauth":
        token = await _fetch_oauth_access_token(auth, timeout_seconds)
        if not token:
            return McpTestResult(
                ok=False,
                error="oauth token fetch failed (check client_id/client_secret/token_url)",
            )
        base_headers["Authorization"] = f"Bearer {token}"

    session_headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": _MCP_PROTOCOL_VERSION,
        **base_headers,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            # 1. initialize
            init_id = 1
            init_resp = await client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": init_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "agent-platform-mcp-registry", "version": "0.1"},
                    },
                },
                headers=session_headers,
            )
            init_resp.raise_for_status()
            mcp_session_id = init_resp.headers.get("Mcp-Session-Id")
            if mcp_session_id:
                session_headers["Mcp-Session-Id"] = mcp_session_id

            init_frame = _parse_response(init_resp, init_id)
            _raise_for_rpc_error(init_frame)
            init_result = init_frame.get("result")
            if not isinstance(init_result, dict):
                return McpTestResult(ok=False, error="initialize returned no result")
            server_info = init_result.get("serverInfo")
            if not isinstance(server_info, dict):
                server_info = {}

            # 2. notifications/initialized - fire and forget; a broken
            #    notification must not fail the whole test.
            with contextlib.suppress(httpx.HTTPError):
                await client.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    },
                    headers=session_headers,
                )

            # 3. tools/list
            list_id = 2
            tools_resp = await client.post(
                endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": list_id,
                    "method": "tools/list",
                    "params": {},
                },
                headers=session_headers,
            )
            tools_resp.raise_for_status()
            tools_frame = _parse_response(tools_resp, list_id)
            _raise_for_rpc_error(tools_frame)
            tools_result = tools_frame.get("result")
            raw_tools = (
                tools_result.get("tools") if isinstance(tools_result, dict) else None
            )

            tools: list[McpTestTool] = []
            if isinstance(raw_tools, list):
                for item in raw_tools:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    description = item.get("description")
                    tools.append(
                        McpTestTool(
                            name=name,
                            description=str(description) if description is not None else None,
                        )
                    )

            return McpTestResult(ok=True, server_info=server_info, tools=tools)
    except httpx.HTTPError as exc:
        return McpTestResult(ok=False, error=f"transport error: {exc}")
    except _McpTestError as exc:
        return McpTestResult(ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - test endpoint must never raise
        return McpTestResult(ok=False, error=f"unexpected error: {exc}")
