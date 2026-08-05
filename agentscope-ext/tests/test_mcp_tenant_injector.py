"""Tests for ``agentscope_ext.mcp_tenant_injector``."""

from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Minimal AgentScope 2.0 MCP stubs
# ---------------------------------------------------------------------------


class _FakeMCPClient:
    """Minimal stub for agentscope.mcp.MCPClient."""

    def __init__(
        self,
        *,
        name: str,
        is_stateful: bool,
        mcp_config,
        enable_tools=None,
        disable_tools=None,
        execution_timeout=None,
    ):
        self.name = name
        self.is_stateful = is_stateful
        self.mcp_config = mcp_config
        self.enable_tools = enable_tools
        self.disable_tools = disable_tools
        self.execution_timeout = execution_timeout
        self._connected = False

    async def connect(self):
        self._connected = True

    async def list_raw_tools(self):
        return []

    @property
    def is_connected(self):
        return self._connected


class _FakeStdioMCPConfig:
    type = "stdio_mcp"

    def __init__(self, *, command, args=None, env=None):
        self.command = command
        self.args = args
        self.env = env


class _FakeHttpMCPConfig:
    type = "http_mcp"

    def __init__(self, *, url, headers=None, timeout=None):
        self.url = url
        self.headers = headers
        self.timeout = timeout


class _FakeToolGroup:
    def __init__(self):
        self.name = "basic"
        self.mcps: list[_FakeMCPClient] = []


class _FakeToolkit:
    """Stub for agentscope.tool.Toolkit.

    Exposes ``tool_groups`` with a single basic ToolGroup that has a
    mutable ``mcps`` list — matching the AgentScope 2.0 structure that
    ``_register_one`` appends to.
    """

    def __init__(self):
        self.tool_groups = [_FakeToolGroup()]

    # Convenience property for assertions
    @property
    def basic_mcps(self) -> list[_FakeMCPClient]:
        return self.tool_groups[0].mcps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh(monkeypatch):
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)

    # Stub agentscope.tool.Toolkit
    tool_mod = types.ModuleType("agentscope.tool")
    tool_mod.Toolkit = _FakeToolkit
    monkeypatch.setitem(sys.modules, "agentscope.tool", tool_mod)

    # Stub agentscope.mcp — MCPClient, StdioMCPConfig, HttpMCPConfig
    mcp_mod = types.ModuleType("agentscope.mcp")
    mcp_mod.MCPClient = _FakeMCPClient
    mcp_mod.StdioMCPConfig = _FakeStdioMCPConfig
    mcp_mod.HttpMCPConfig = _FakeHttpMCPConfig
    monkeypatch.setitem(sys.modules, "agentscope.mcp", mcp_mod)

    # Also stub the parent package so sub-imports resolve
    as_mod = sys.modules.get("agentscope") or types.ModuleType("agentscope")
    as_mod.mcp = mcp_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentscope", as_mod)

    import agentscope_ext.mcp_tenant_injector as ext
    importlib.reload(ext)
    ext._INSTALLED = False
    return hooks_mod, ext


def _env(*, include_service_scopes: bool = True):
    env = {
        # MCP_REGISTRY_URL is the preferred env var (passed by the scheduler);
        # FOUNDATION_API_URL remains as a fallback for legacy deployments.
        "MCP_REGISTRY_URL": "https://mcp-registry.local/api/mcp/v1",
        "FOUNDATION_API_URL": "https://foundation.local",
        "FOUNDATION_API_TOKEN": "tok-xyz",
        "TOOL_BROKER_LEASE": "v1.payload.signature",
    }
    if include_service_scopes:
        # Scheduler injects this so the extension can set X-Service-Scopes.
        env["FOUNDATION_SERVICE_SCOPES"] = "mcp.read_internal,skill.read_internal"
    return env


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_install_registers_each_server(fresh):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        # injector appends /internal/mcp-servers to MCP_REGISTRY_URL
        assert request.url.path == "/api/mcp/v1/internal/mcp-servers"
        assert request.url.params["tenant_id"] == "tnt-abc"
        assert request.headers["X-Tenant-Id"] == "tnt-abc"
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "github",
                        "transport": "stdio",
                        "command": "/usr/bin/github-mcp",
                        "env": {"GH_TOKEN": "xxx"},
                    },
                    {
                        "name": "filesystem",
                        "transport": "http",
                        "endpoint": "https://fs.local/mcp",
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    assert isinstance(toolkit, _FakeToolkit)
    clients = toolkit.basic_mcps
    assert len(clients) == 2

    # github — stdio (stateful)
    gh = clients[0]
    assert gh.name == "github"
    assert gh.is_stateful is True
    assert gh._connected is True  # connect() was called
    assert isinstance(gh.mcp_config, _FakeStdioMCPConfig)
    assert gh.mcp_config.command == "/usr/bin/github-mcp"
    assert gh.mcp_config.env == {"GH_TOKEN": "xxx"}

    # filesystem — http (stateless)
    fs = clients[1]
    assert fs.name == "filesystem"
    assert fs.is_stateful is False
    assert isinstance(fs.mcp_config, _FakeHttpMCPConfig)
    assert fs.mcp_config.url == "https://fs.local/mcp"

    injector.set_client(None)


def test_sse_server_is_skipped(fresh, caplog):
    """Legacy SSE servers are not supported by the agentscope HTTP client
    (Streamable HTTP only); they are skipped with a warning rather than
    handed to HttpMCPConfig where the probe would fail. The SDK worker direct
    path supports SSE natively."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "legacy-sse",
                        "transport": "sse",
                        "endpoint": "https://legacy.local/sse",
                    },
                    {
                        "name": "ok-http",
                        "transport": "http",
                        "url": "https://ok.local/mcp",
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    # Only the http server is registered; the sse server is skipped.
    assert [c.name for c in toolkit.basic_mcps] == ["ok-http"]
    assert any(
        "skipping SSE server" in r.message and "legacy-sse" in r.message
        for r in caplog.records
    )

    injector.set_client(None)


def test_sse_server_skipped_in_broker_mode(fresh, caplog):
    """SSE servers are skipped even in broker mode - the broker passthrough
    is streamable-http only, so sse via broker does not work either."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mcp_SSE1",
                        "name": "legacy-sse",
                        "transport": "sse",
                        "endpoint": "https://legacy.local/sse",
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    env = _env()
    env.update(
        {
            "SANDBOX_EGRESS_MODE": "broker",
            "TOOL_BROKER_URL": "http://tool-broker:8024/api/tool-broker/v1",
            "FOUNDATION_TENANT_ID": "tnt-abc",
        }
    )
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", env)

    assert toolkit.basic_mcps == []
    assert any(
        "skipping SSE server" in r.message and "legacy-sse" in r.message
        for r in caplog.records
    )

    injector.set_client(None)


def test_fetch_sends_x_service_scopes_header(fresh):
    """The injector must forward FOUNDATION_SERVICE_SCOPES as X-Service-Scopes.

    Without this header the mcp-registry internal endpoint returns 403.
    The scheduler injects FOUNDATION_SERVICE_SCOPES into the sandbox env so
    the extension can assert the required scope without a gateway in dev.
    """
    hooks_mod, ext = fresh
    captured_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json={"servers": []})

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    hooks_mod.build_toolkit({}, "tnt-abc", _env(include_service_scopes=True))

    assert "x-service-scopes" in captured_headers, (
        "X-Service-Scopes header not sent to mcp-registry internal endpoint"
    )
    assert "mcp.read_internal" in captured_headers["x-service-scopes"]
    assert captured_headers.get("x-tenant-id") == "tnt-abc"
    assert captured_headers.get("x-tool-broker-lease") == "v1.payload.signature"

    injector.set_client(None)


def test_fetch_without_service_scopes_omits_header(fresh):
    """When FOUNDATION_SERVICE_SCOPES is absent the header must be omitted.

    This ensures we don't send a stale/wrong scope value; the request will
    get a 403 from the service which is the correct behaviour when no gateway
    has validated the caller.
    """
    hooks_mod, ext = fresh
    captured_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(403, json={"error": "no scope"})

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    # 403 is swallowed by the extension's broad except → base toolkit returned.
    hooks_mod.build_toolkit({}, "tnt-abc", _env(include_service_scopes=False))

    assert "x-service-scopes" not in captured_headers

    injector.set_client(None)


def test_registry_5xx_returns_base_toolkit(fresh, caplog):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    assert isinstance(toolkit, _FakeToolkit)
    assert toolkit.basic_mcps == []
    assert any("registry fetch failed" in r.message for r in caplog.records)

    injector.set_client(None)


def test_install_is_idempotent(fresh):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"servers": []})

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    wrapped_once = hooks_mod.build_toolkit
    ext.install()
    assert hooks_mod.build_toolkit is wrapped_once

    injector.set_client(None)


def test_bad_server_entry_does_not_break_others(fresh, caplog):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {"transport": "stdio"},  # missing 'name' — should be skipped
                    {"name": "ok", "transport": "stdio", "command": "/bin/ok"},
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    assert [c.name for c in toolkit.basic_mcps] == ["ok"]
    assert any("register failed" in r.message for r in caplog.records)

    injector.set_client(None)


def test_http_server_uses_url_field(fresh):
    """Servers with transport=http and a 'url' field (not 'endpoint') work."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "weather",
                        "transport": "http",
                        "url": "https://weather.mcp.local/mcp",
                        "headers": {"X-Api-Key": "k1"},
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    clients = toolkit.basic_mcps
    assert len(clients) == 1
    w = clients[0]
    assert w.name == "weather"
    assert w.is_stateful is False
    assert isinstance(w.mcp_config, _FakeHttpMCPConfig)
    assert w.mcp_config.url == "https://weather.mcp.local/mcp"
    assert w.mcp_config.headers == {"X-Api-Key": "k1"}

    injector.set_client(None)


def test_broker_mode_rewrites_http_server_to_tenant_passthrough(fresh):
    """Broker-only sandboxes must never connect to the raw MCP endpoint."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mcp_ABC123",
                        "name": "echo",
                        "transport": "http",
                        "endpoint": "http://echo-mcp:9000/mcp",
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    env = _env()
    env.update(
        {
            "SANDBOX_EGRESS_MODE": "broker",
            "TOOL_BROKER_URL": "http://tool-broker:8024/api/tool-broker/v1/",
            "FOUNDATION_TENANT_ID": "tnt-abc",
        }
    )
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", env)

    assert len(toolkit.basic_mcps) == 1
    config = toolkit.basic_mcps[0].mcp_config
    assert config.url == (
        "http://tool-broker:8024/api/tool-broker/v1/"
        "mcp-tenant/v1/mcp_ABC123"
    )
    assert config.headers == {
        "Authorization": "Bearer tok-xyz",
        "X-Tenant-Id": "tnt-abc",
        "X-Tool-Broker-Lease": "v1.payload.signature",
    }

    injector.set_client(None)


def test_broker_mode_drops_http_server_without_routing_context(fresh, caplog):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mcp_ABC123",
                        "name": "echo",
                        "transport": "http",
                        "endpoint": "http://echo-mcp:9000/mcp",
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    env = _env()
    env.update(
        {
            "SANDBOX_EGRESS_MODE": "broker",
            "FOUNDATION_TENANT_ID": "tnt-abc",
        }
    )
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", env)

    assert toolkit.basic_mcps == []
    assert any("missing server id / tool-broker url" in r.message for r in caplog.records)

    injector.set_client(None)


def test_stdio_server_missing_command_skipped(fresh, caplog):
    """A stdio server with no 'command' field is skipped with a warning."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {"name": "broken", "transport": "stdio"},  # no command
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    assert toolkit.basic_mcps == []
    assert any("register failed" in r.message for r in caplog.records)

    injector.set_client(None)


def test_tool_filter_maps_to_enable_disable_tools(fresh):
    """registry tool_filter {allow, block} -> MCPClient enable/disable_tools.

    allow with block subtracted (so the two never overlap - AgentScope's
    MCPClient raises ValueError on overlap); a tool in both allow and block
    ends up disabled.
    """
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "s1",
                        "transport": "http",
                        "url": "https://s1/mcp",
                        "tool_filter": {"allow": ["a", "b"], "block": ["secret"]},
                    },
                    {
                        "name": "s2",
                        "transport": "http",
                        "url": "https://s2/mcp",
                        # overlap on "y" -> y removed from enable, kept in disable
                        "tool_filter": {"allow": ["x", "y"], "block": ["y", "z"]},
                    },
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    by_name = {c.name: c for c in toolkit.basic_mcps}
    assert by_name["s1"].enable_tools == ["a", "b"]
    assert by_name["s1"].disable_tools == ["secret"]
    assert by_name["s2"].enable_tools == ["x"]  # "y" subtracted (overlap)
    assert by_name["s2"].disable_tools == ["y", "z"]

    injector.set_client(None)


def test_timeout_seconds_applied_to_http_config_and_execution_timeout(fresh):
    """timeout_seconds -> HttpMCPConfig.timeout (connect+read) + execution_timeout."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "echo",
                        "transport": "http",
                        "url": "https://echo/mcp",
                        "timeout_seconds": 42,
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    c = toolkit.basic_mcps[0]
    assert c.mcp_config.timeout == 42
    assert c.execution_timeout == 42

    injector.set_client(None)


def test_timeout_seconds_applied_to_stdio_execution_timeout(fresh):
    """stdio has no request-timeout field; timeout_seconds -> execution_timeout only."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "local",
                        "transport": "stdio",
                        "command": "run-local",
                        "timeout_seconds": 15,
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    c = toolkit.basic_mcps[0]
    assert isinstance(c.mcp_config, _FakeStdioMCPConfig)
    assert c.execution_timeout == 15

    injector.set_client(None)


def test_direct_mode_injects_registry_auth_headers(fresh):
    """Non-broker HTTP: the injector injects registry auth + headers toward the upstream."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "name": "echo",
                        "transport": "http",
                        "url": "https://echo/mcp",
                        "headers": {"X-Custom": "reg"},
                        "auth": {"type": "bearer", "token": "upstream-tok"},
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    # _env() does not set SANDBOX_EGRESS_MODE=broker -> direct (non-broker) mode.
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    c = toolkit.basic_mcps[0]
    assert c.mcp_config.headers == {
        "X-Custom": "reg",
        "Authorization": "Bearer upstream-tok",
    }

    injector.set_client(None)


def test_broker_mode_drops_registry_auth_and_headers(fresh):
    """Broker mode: registry upstream auth/headers are dropped (broker injects them)."""
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mcp_X",
                        "name": "echo",
                        "transport": "http",
                        "endpoint": "http://echo-mcp:9000/mcp",
                        "headers": {"X-Custom": "reg"},
                        "auth": {"type": "bearer", "token": "upstream-tok"},
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    env = _env()
    env.update(
        {
            "SANDBOX_EGRESS_MODE": "broker",
            "TOOL_BROKER_URL": "http://tool-broker:8024/api/tool-broker/v1",
            "FOUNDATION_TENANT_ID": "tnt-abc",
        }
    )
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", env)

    c = toolkit.basic_mcps[0]
    # Only broker-auth headers survive; the registry's upstream auth/headers
    # are dropped (the broker re-injects them toward the real upstream).
    assert c.mcp_config.headers == {
        "Authorization": "Bearer tok-xyz",
        "X-Tenant-Id": "tnt-abc",
        "X-Tool-Broker-Lease": "v1.payload.signature",
    }

    injector.set_client(None)


# ---------------------------------------------------------------------------
# OAuth client-credentials auth (direct-mode MCP servers)
# ---------------------------------------------------------------------------
#
# In DIRECT (non-broker) mode the injector performs the client-credentials flow
# itself and injects the bearer.  Broker mode pops ``auth`` (so no oauth fetch
# happens - the broker does it).  These tests stub ``_fetch_oauth_token`` to
# avoid real HTTP for the integration / cache logic, plus one direct test of the
# POST body shape.


def _oauth_auth(scopes=None) -> dict:
    auth = {
        "type": "oauth",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://idp.example/oauth/token",
    }
    if scopes is not None:
        auth["scopes"] = scopes
    return auth


def test_direct_mode_oauth_fetches_token_and_injects_bearer(fresh, monkeypatch):
    """oauth auth in direct mode: injector fetches a token and injects Bearer."""
    hooks_mod, ext = fresh
    ext._OAUTH_TOKEN_CACHE.clear()
    fetch_calls: list = []

    def fake_fetch(auth):
        fetch_calls.append(auth)
        return "tok-oauth", time.time() + 3600

    monkeypatch.setattr(ext, "_fetch_oauth_token", fake_fetch)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "mcp_o1",
                        "name": "echo",
                        "transport": "http",
                        "url": "https://echo/mcp",
                        "auth": _oauth_auth(scopes=["a", "b"]),
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    c = toolkit.basic_mcps[0]
    assert c.mcp_config.headers == {"Authorization": "Bearer tok-oauth"}
    # The token was fetched exactly once with the oauth auth record.
    assert len(fetch_calls) == 1
    assert fetch_calls[0]["client_id"] == "cid"
    assert fetch_calls[0]["scopes"] == ["a", "b"]

    injector.set_client(None)


def test_direct_mode_oauth_fetch_failure_is_fail_open(fresh, monkeypatch, caplog):
    """A token-endpoint failure is fail-open: no Authorization header, server
    still registered (the MCPClient probe surfaces the upstream's own 401)."""
    hooks_mod, ext = fresh
    ext._OAUTH_TOKEN_CACHE.clear()

    def boom(auth):
        raise RuntimeError("idp unreachable")

    monkeypatch.setattr(ext, "_fetch_oauth_token", boom)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "mcp_o_fail",
                        "name": "echo",
                        "transport": "http",
                        "url": "https://echo/mcp",
                        "auth": _oauth_auth(),
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())

    # Fail-open: no Authorization header injected -> headers is None (empty dict
    # collapses to None via ``headers or None`` in _build_mcp_client).
    c = toolkit.basic_mcps[0]
    assert c.mcp_config.headers is None
    assert any("oauth token fetch failed" in r.message for r in caplog.records)

    injector.set_client(None)


def test_broker_mode_does_not_fetch_oauth_token(fresh, monkeypatch):
    """Broker mode pops ``auth`` -> the injector never fetches an oauth token
    (the broker is the single upstream-auth injection point)."""
    hooks_mod, ext = fresh
    ext._OAUTH_TOKEN_CACHE.clear()
    fetch_calls: list = []

    def fake_fetch(auth):
        fetch_calls.append(auth)
        return "should-not-be-called", time.time() + 3600

    monkeypatch.setattr(ext, "_fetch_oauth_token", fake_fetch)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "mcp_o_broker",
                        "name": "echo",
                        "transport": "http",
                        "endpoint": "http://echo-mcp:9000/mcp",
                        "auth": _oauth_auth(),
                    }
                ]
            },
        )

    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    env = _env()
    env.update(
        {
            "SANDBOX_EGRESS_MODE": "broker",
            "TOOL_BROKER_URL": "http://tool-broker:8024/api/tool-broker/v1",
            "FOUNDATION_TENANT_ID": "tnt-abc",
        }
    )
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", env)

    # No oauth fetch happened (broker mode popped auth).
    assert fetch_calls == []
    c = toolkit.basic_mcps[0]
    assert c.mcp_config.headers == {
        "Authorization": "Bearer tok-xyz",
        "X-Tenant-Id": "tnt-abc",
        "X-Tool-Broker-Lease": "v1.payload.signature",
    }

    injector.set_client(None)


def test_oauth_fetch_posts_client_credentials_form(fresh, monkeypatch):
    """_fetch_oauth_token POSTs the client_credentials form with space-joined
    scopes and parses {access_token, expires_in}."""
    hooks_mod, ext = fresh
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"access_token": "tok", "expires_in": 7200}

    def fake_post(url, *, data, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr("httpx.post", fake_post)

    token, expires_at = ext._fetch_oauth_token(_oauth_auth(scopes=["a", "b"]))
    assert token == "tok"
    assert captured["url"] == "https://idp.example/oauth/token"
    assert captured["data"]["grant_type"] == "client_credentials"
    assert captured["data"]["client_id"] == "cid"
    assert captured["data"]["client_secret"] == "csec"
    assert captured["data"]["scope"] == "a b"
    assert captured["timeout"] == ext._OAUTH_TOKEN_TIMEOUT_SECONDS
    assert expires_at > time.time() + 7000


def test_oauth_fetch_defaults_expires_in(fresh, monkeypatch):
    """A missing ``expires_in`` defaults to 3600s (no crash)."""
    hooks_mod, ext = fresh

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"access_token": "tok"}  # no expires_in

    monkeypatch.setattr("httpx.post", lambda *a, **k: _FakeResp())

    token, expires_at = ext._fetch_oauth_token(_oauth_auth())
    assert token == "tok"
    assert expires_at > time.time()


def test_oauth_get_token_caches_valid_token(fresh, monkeypatch):
    """A cached token within its refresh margin is reused without refetch."""
    hooks_mod, ext = fresh
    ext._OAUTH_TOKEN_CACHE.clear()
    calls = 0

    def fake_fetch(auth):
        nonlocal calls
        calls += 1
        return f"tok-{calls}", time.time() + 3600

    monkeypatch.setattr(ext, "_fetch_oauth_token", fake_fetch)
    auth = _oauth_auth()

    first = ext._get_oauth_token("srv_cache", auth)
    second = ext._get_oauth_token("srv_cache", auth)
    assert calls == 1
    assert first == second == "tok-1"


def test_oauth_get_token_refreshes_after_expiry(fresh, monkeypatch):
    """A cached token past its refresh margin is refetched."""
    hooks_mod, ext = fresh
    ext._OAUTH_TOKEN_CACHE.clear()
    calls = 0

    def fake_fetch(auth):
        nonlocal calls
        calls += 1
        return f"tok-{calls}", time.time() - 1  # already expired

    monkeypatch.setattr(ext, "_fetch_oauth_token", fake_fetch)
    auth = _oauth_auth()

    first = ext._get_oauth_token("srv_refresh", auth)
    second = ext._get_oauth_token("srv_refresh", auth)
    assert calls == 2
    assert first == "tok-1"
    assert second == "tok-2"


# ---------------------------------------------------------------------------
# Run-level MCP selection (task.json "mcp_server_ids") — three-state
# ---------------------------------------------------------------------------
#
# The scheduler writes ``mcp_server_ids`` into task.json, which reaches this
# hook as its ``task`` argument.  Presence is load-bearing:
#   key absent -> tenant default (all enabled servers)
#   []         -> MCP disabled for this run
#   non-empty  -> only those server ids
# ``[]`` is falsy, so any truthiness-based reader would collapse the first two.


def _two_server_handler(seen: list[httpx.Request] | None = None):
    """Registry stub returning two http servers with distinct ids."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "mcp_AAA",
                        "name": "alpha",
                        "transport": "http",
                        "url": "https://alpha.local/mcp",
                    },
                    {
                        "id": "mcp_BBB",
                        "name": "beta",
                        "transport": "http",
                        "url": "https://beta.local/mcp",
                    },
                ]
            },
        )

    return _handler


def _build_with_selection(fresh, task: dict, seen=None):
    """Install the injector against the two-server registry and build a toolkit."""
    hooks_mod, ext = fresh
    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(
        httpx.Client(transport=httpx.MockTransport(_two_server_handler(seen)))
    )
    try:
        ext.install()
        return hooks_mod.build_toolkit(task, "tnt-abc", _env())
    finally:
        injector.set_client(None)


def test_mcp_server_ids_absent_registers_all(fresh):
    """Key absent => tenant default. Regression guard for the pre-existing
    auto-inject-everything behaviour."""
    toolkit = _build_with_selection(fresh, {"task": "hi"})
    assert [c.name for c in toolkit.basic_mcps] == ["alpha", "beta"]


def test_mcp_server_ids_empty_registers_none(fresh):
    """Explicit [] => MCP off for this run, and the registry is not even
    called (nothing could survive the filter)."""
    seen: list[httpx.Request] = []
    toolkit = _build_with_selection(
        fresh, {"task": "hi", "mcp_server_ids": []}, seen=seen
    )
    assert toolkit.basic_mcps == []
    assert seen == []


def test_mcp_server_ids_subset_registers_only_selected(fresh):
    """Non-empty list => only the named ids are registered."""
    toolkit = _build_with_selection(
        fresh, {"task": "hi", "mcp_server_ids": ["mcp_BBB"]}
    )
    assert [c.name for c in toolkit.basic_mcps] == ["beta"]


def test_mcp_server_ids_unknown_id_registers_none(fresh):
    """Ids that match no registry record simply select nothing."""
    toolkit = _build_with_selection(
        fresh, {"task": "hi", "mcp_server_ids": ["mcp_NOPE"]}
    )
    assert toolkit.basic_mcps == []


def test_mcp_server_ids_malformed_falls_back_to_tenant_default(fresh):
    """A present-but-non-list value degrades to 'absent' (tenant default)
    rather than to 'disable everything'."""
    toolkit = _build_with_selection(
        fresh, {"task": "hi", "mcp_server_ids": "mcp_AAA"}
    )
    assert [c.name for c in toolkit.basic_mcps] == ["alpha", "beta"]


def test_legacy_mcp_servers_key_is_not_the_selector(fresh):
    """The legacy ``mcp_servers`` key (hardcoded to [] by the scheduler) must
    NOT be mistaken for the selection - reading it would disable MCP on every
    run."""
    toolkit = _build_with_selection(fresh, {"task": "hi", "mcp_servers": []})
    assert [c.name for c in toolkit.basic_mcps] == ["alpha", "beta"]


def test_selected_server_ids_presence_semantics(fresh):
    """Unit-level three-state check on the reader itself."""
    _hooks_mod, ext = fresh
    assert ext._selected_server_ids({}) is None
    assert ext._selected_server_ids({"mcp_server_ids": None}) is None
    assert ext._selected_server_ids({"mcp_server_ids": []}) == []
    assert ext._selected_server_ids({"mcp_server_ids": ["a", "b"]}) == ["a", "b"]
    # blanks / non-strings are dropped, but the list stays a list
    assert ext._selected_server_ids({"mcp_server_ids": [" a ", "", 7]}) == ["a"]


def test_selection_runs_before_transport_filter(fresh, caplog):
    """Selection is applied BEFORE the SSE/transport drop: deselecting an SSE
    server must not emit the 'skipping SSE server' warning, because the server
    was never a candidate for this run."""
    hooks_mod, ext = fresh
    from agentscope_ext.mcp_tenant_injector import injector

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "servers": [
                    {
                        "id": "mcp_SSE",
                        "name": "legacy-sse",
                        "transport": "sse",
                        "endpoint": "https://legacy.local/sse",
                    },
                    {
                        "id": "mcp_AAA",
                        "name": "alpha",
                        "transport": "http",
                        "url": "https://alpha.local/mcp",
                    },
                ]
            },
        )

    injector.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    try:
        ext.install()
        with caplog.at_level("WARNING"):
            toolkit = hooks_mod.build_toolkit(
                {"task": "hi", "mcp_server_ids": ["mcp_AAA"]}, "tnt-abc", _env()
            )
    finally:
        injector.set_client(None)

    assert [c.name for c in toolkit.basic_mcps] == ["alpha"]
    assert "skipping SSE server" not in caplog.text
