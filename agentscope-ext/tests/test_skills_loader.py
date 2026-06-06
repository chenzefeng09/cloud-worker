"""Tests for ``agentscope_ext.skills_loader``."""

from __future__ import annotations

import hashlib
import importlib
import io
import sys
import tarfile
import types
from pathlib import Path

import httpx
import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


class _FakeToolGroup:
    def __init__(self):
        self.name = "basic"
        self.tools: list = []
        self.mcps: list = []


class _RecordingToolkit:
    def __init__(self):
        # AgentScope 2.0: tool_groups[0].mcps is where MCPClient objects live.
        # tool_groups[0].tools is where register_into appends built-in tools.
        self.tool_groups = [_FakeToolGroup()]

    @property
    def tools(self) -> list:
        """Unified view: both register_into (tool_groups[0].tools) and
        register_tool_function (used by skill lambdas) land here."""
        return self.tool_groups[0].tools

    def register_tool_function(self, fn, **_):
        self.tools.append(fn)

    @property
    def basic_mcps(self) -> list:
        return self.tool_groups[0].mcps


class _FakeMCPClient:
    """Minimal stub for agentscope.mcp.MCPClient (used in chain test)."""

    def __init__(self, *, name: str, is_stateful: bool, mcp_config):
        self.name = name
        self.is_stateful = is_stateful
        self.mcp_config = mcp_config
        self._connected = False

    async def connect(self):
        self._connected = True

    async def list_raw_tools(self):
        return []


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


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)

    tool_mod = types.ModuleType("agentscope.tool")
    tool_mod.Toolkit = _RecordingToolkit

    # Stub Read/Grep/Glob so register_into succeeds and the tools land in
    # toolkit.tools via register_tool_function (the _RecordingToolkit path).
    class _FakeRead:
        pass

    class _FakeGrep:
        pass

    class _FakeGlob:
        pass

    tool_mod.Read = _FakeRead
    tool_mod.Grep = _FakeGrep
    tool_mod.Glob = _FakeGlob

    monkeypatch.setitem(sys.modules, "agentscope.tool", tool_mod)

    # Stub agentscope.mcp so mcp_tenant_injector can import it without
    # the real agentscope package being installed on the host.
    mcp_mod = types.ModuleType("agentscope.mcp")
    mcp_mod.MCPClient = _FakeMCPClient
    mcp_mod.StdioMCPConfig = _FakeStdioMCPConfig
    mcp_mod.HttpMCPConfig = _FakeHttpMCPConfig
    monkeypatch.setitem(sys.modules, "agentscope.mcp", mcp_mod)
    as_mod = sys.modules.get("agentscope") or types.ModuleType("agentscope")
    as_mod.mcp = mcp_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agentscope", as_mod)

    import agentscope_ext.skills_loader as ext
    importlib.reload(ext)
    ext._INSTALLED = False

    from agentscope_ext.skills_loader import loader
    loader.set_skills_root(tmp_path / "skills")
    loader._INSTALLED_PATHS.clear()
    return hooks_mod, ext, loader, tmp_path


def _make_tarball(entry_module: str, body_src: str) -> bytes:
    """Build a gzipped tarball containing ``<entry_module>.py`` with ``body_src``."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        data = body_src.encode("utf-8")
        info = tarfile.TarInfo(name=f"{entry_module}.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _env(*, include_service_scopes: bool = True):
    env = {
        # Dedicated service URLs (preferred; passed by the scheduler).
        "MCP_REGISTRY_URL": "https://mcp-registry.local/api/mcp/v1",
        "SKILL_STORE_URL": "https://skill-store.local/api/skills/v1",
        # Fallback for legacy single-gateway deployments.
        "FOUNDATION_API_URL": "https://foundation.local",
        "FOUNDATION_API_TOKEN": "tok-xyz",
    }
    if include_service_scopes:
        # Scheduler injects this so the extension can set X-Service-Scopes.
        env["FOUNDATION_SERVICE_SCOPES"] = "mcp.read_internal,skill.read_internal"
    return env


def test_install_downloads_and_registers_tool(fresh):
    hooks_mod, ext, loader, tmp_path = fresh

    src = (
        "def register(toolkit):\n"
        "    toolkit.register_tool_function(lambda: 'csv-tool-ran')\n"
    )
    tarball = _make_tarball("csv_tools_skill", src)
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        # loader appends /internal/skills to SKILL_STORE_URL
        if request.url.path == "/api/skills/v1/internal/skills":
            assert request.url.params["tenant_id"] == "tnt-abc"
            assert request.headers["X-Tenant-Id"] == "tnt-abc"
            assert "csv-tools" in request.url.params.get_list("names")
            return httpx.Response(
                200,
                json={
                    "skills": [
                        {
                            "name": "csv-tools",
                            "version": "1.2.0",
                            "download_url": "https://cdn.local/csv-tools.tgz",
                            "sha256": sha,
                            "entry_module": "csv_tools_skill",
                            "entry_function": "register",
                            "content_type": "application/x-gzip",
                        }
                    ]
                },
            )
        if request.url.host == "cdn.local":
            return httpx.Response(
                200,
                content=tarball,
                headers={"content-type": "application/x-gzip"},
            )
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    toolkit = hooks_mod.build_toolkit(
        {"skills": ["csv-tools"]}, "tnt-abc", _env(),
    )
    # 3 built-in workspace tools (Read, Grep, Glob) + 1 downloaded skill tool
    assert len(toolkit.tools) == 4
    # The built-in tools are instances of _FakeRead/_FakeGrep/_FakeGlob (not callables).
    # The skill lambda is callable and returns "csv-tool-ran".
    skill_lambdas = [t for t in toolkit.tools if callable(t)]
    assert len(skill_lambdas) == 1
    assert skill_lambdas[0]() == "csv-tool-ran"

    loader.set_client(None)


def test_sha256_mismatch_is_skipped(fresh, caplog):
    hooks_mod, ext, loader, tmp_path = fresh

    src = "def register(toolkit):\n    toolkit.register_tool_function(lambda: 1)\n"
    tarball = _make_tarball("bad_sha_skill", src)

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            return httpx.Response(
                200,
                json={
                    "skills": [
                        {
                            "name": "evil",
                            "version": "0.0.1",
                            "download_url": "https://cdn.local/evil.tgz",
                            "sha256": "0" * 64,  # bogus
                            "entry_module": "bad_sha_skill",
                            "entry_function": "register",
                            "content_type": "application/x-gzip",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, content=tarball, headers={"content-type": "application/x-gzip"},
        )

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit(
            {"skills": ["evil"]}, "tnt-abc", _env(),
        )
    # Only the 3 built-in workspace tools (Read/Grep/Glob); SHA-mismatched skill rejected.
    # No extra (malicious) skill tool beyond the 3 built-ins.
    assert len(toolkit.tools) == 3
    assert any("sha256 mismatch" in r.message for r in caplog.records)

    loader.set_client(None)


def test_no_skills_in_task_is_passthrough(fresh):
    hooks_mod, ext, loader, _ = fresh
    ext.install()
    # No skills key → no HTTP traffic, base toolkit returned with built-in tools.
    toolkit = hooks_mod.build_toolkit({}, "tnt-abc", _env())
    assert isinstance(toolkit, _RecordingToolkit)
    # 3 built-in workspace tools (Read/Grep/Glob) are always present.
    assert len(toolkit.tools) == 3


def test_wrong_content_type_is_skipped(fresh, caplog):
    hooks_mod, ext, loader, _ = fresh

    tarball = b"not really gzipped"

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            return httpx.Response(
                200,
                json={
                    "skills": [
                        {
                            "name": "weird",
                            "version": "0.0.1",
                            "download_url": "https://cdn.local/x",
                            "entry_module": "weird_skill",
                            "entry_function": "register",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, content=tarball, headers={"content-type": "application/zip"},
        )

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        toolkit = hooks_mod.build_toolkit(
            {"skills": ["weird"]}, "tnt-abc", _env(),
        )
    # Only the 3 built-in workspace tools; the wrong-content-type skill is rejected.
    assert len(toolkit.tools) == 3
    assert any("content-type" in r.message for r in caplog.records)

    loader.set_client(None)


def test_chained_with_mcp_injector_preserves_both(fresh, monkeypatch):
    """When mcp_tenant_injector and skills_loader are both installed,
    the toolkit produced should have *both* MCP servers and skill tools."""
    hooks_mod, ext, loader, _ = fresh

    # Bring mcp injector in too.
    import agentscope_ext.mcp_tenant_injector as mcp_ext
    importlib.reload(mcp_ext)
    mcp_ext._INSTALLED = False

    src = "def register(toolkit):\n    toolkit.register_tool_function(lambda: 'skill')\n"
    tarball = _make_tarball("chain_skill", src)
    sha = hashlib.sha256(tarball).hexdigest()

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/mcp/v1/internal/mcp-servers":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"name": "fs", "transport": "stdio", "command": "/bin/fs"},
                    ]
                },
            )
        if request.url.path == "/api/skills/v1/internal/skills":
            return httpx.Response(
                200,
                json={
                    "skills": [
                        {
                            "name": "chain-skill",
                            "version": "0.0.1",
                            "download_url": "https://cdn.local/x",
                            "sha256": sha,
                            "entry_module": "chain_skill",
                            "entry_function": "register",
                            "content_type": "application/x-gzip",
                        }
                    ]
                },
            )
        return httpx.Response(
            200, content=tarball, headers={"content-type": "application/x-gzip"},
        )

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    from agentscope_ext.mcp_tenant_injector import injector

    injector.set_client(client)
    loader.set_client(client)

    # Order matches the canonical enable_all(): MCP first, Skills on top.
    mcp_ext.install()
    ext.install()

    toolkit = hooks_mod.build_toolkit(
        {"skills": ["chain-skill"]}, "tnt-abc", _env(),
    )
    # AgentScope 2.0: MCPClient objects live in tool_groups[0].mcps.
    assert [c.name for c in toolkit.basic_mcps] == ["fs"]
    # 3 built-in workspace tools (Read/Grep/Glob) + 1 downloaded skill tool
    assert len(toolkit.tools) == 4
    # The skill lambda is callable; built-in instances are not.
    skill_lambdas = [t for t in toolkit.tools if callable(t)]
    assert len(skill_lambdas) == 1
    assert skill_lambdas[0]() == "skill"

    injector.set_client(None)
    loader.set_client(None)


def test_fetch_sends_x_service_scopes_header(fresh):
    """The loader must forward FOUNDATION_SERVICE_SCOPES as X-Service-Scopes.

    Without this header the skill-store internal endpoint returns 403.
    The scheduler injects FOUNDATION_SERVICE_SCOPES into the sandbox env so
    the extension can assert the required scope without a gateway in dev.
    """
    hooks_mod, ext, loader, _ = fresh
    captured_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            captured_headers.update(dict(request.headers))
            return httpx.Response(200, json={"skills": []})
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    hooks_mod.build_toolkit(
        {"skills": ["csv-tools"]}, "tnt-abc", _env(include_service_scopes=True),
    )

    assert "x-service-scopes" in captured_headers, (
        "X-Service-Scopes header not sent to skill-store internal endpoint"
    )
    assert "skill.read_internal" in captured_headers["x-service-scopes"]
    assert captured_headers.get("x-tenant-id") == "tnt-abc"

    loader.set_client(None)


def test_fetch_without_service_scopes_omits_header(fresh, caplog):
    """When FOUNDATION_SERVICE_SCOPES is absent the header must be omitted."""
    hooks_mod, ext, loader, _ = fresh
    captured_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/skills/v1/internal/skills":
            captured_headers.update(dict(request.headers))
            return httpx.Response(403, json={"error": "no scope"})
        return httpx.Response(404)

    loader.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    with caplog.at_level("WARNING"):
        hooks_mod.build_toolkit(
            {"skills": ["csv-tools"]}, "tnt-abc", _env(include_service_scopes=False),
        )

    assert "x-service-scopes" not in captured_headers
    assert any("manifest fetch failed" in r.message for r in caplog.records)

    loader.set_client(None)
