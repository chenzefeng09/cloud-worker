"""Tests for ``agentscope_ext.mcp_tenant_injector``."""

from __future__ import annotations

import importlib
import sys
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

    def __init__(self, *, name: str, is_stateful: bool, mcp_config):
        self.name = name
        self.is_stateful = is_stateful
        self.mcp_config = mcp_config
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
                        "transport": "sse",
                        "endpoint": "https://fs.local/sse",
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

    # filesystem — sse (stateless)
    fs = clients[1]
    assert fs.name == "filesystem"
    assert fs.is_stateful is False
    assert isinstance(fs.mcp_config, _FakeHttpMCPConfig)
    assert fs.mcp_config.url == "https://fs.local/sse"

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
