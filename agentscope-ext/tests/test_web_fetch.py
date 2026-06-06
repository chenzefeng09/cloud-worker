"""Tests for ``agentscope_ext.web_fetch``.

Covers:
- WebFetchTool returns formatted success text on HTTP 200.
- WebFetchTool returns ``[WebFetch blocked]`` on HTTP 403.
- WebFetchTool returns ``[WebFetch error]`` when the gateway is unreachable
  (ConnectError).
- WebFetchTool returns ``[WebFetch error]`` on timeout.
- WebFetchTool appends a truncation note when ``truncated=True``.
- install() wraps hooks.build_toolkit and appends the tool when
  WEB_FETCH_GATEWAY_URL is set.
- install() is idempotent (second call is a no-op).
- install() skips tool registration (with a warning) when
  WEB_FETCH_GATEWAY_URL is absent.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Toolkit / ToolGroup stubs (same pattern as test_workspace_tools.py)
# ---------------------------------------------------------------------------


class _FakeToolGroup:
    def __init__(self):
        self.name = "basic"
        self.tools: list = []
        self.mcps: list = []


class _FakeToolkit:
    def __init__(self):
        self.tool_groups = [_FakeToolGroup()]

    @property
    def tools(self):
        return self.tool_groups[0].tools


# ---------------------------------------------------------------------------
# WebFetchTool unit tests (mock httpx)
# ---------------------------------------------------------------------------


class TestWebFetchTool:
    """Direct tests against WebFetchTool.__call__ with mocked httpx."""

    def _make_tool(self):
        from agentscope_ext.web_fetch.tool import WebFetchTool

        return WebFetchTool(
            gateway_url="http://fake-gateway:8013",
            token="tok-web",
            tenant_id="tnt-web",
            user_id="usr-web",
            service_scopes="mcp.read_internal,skill.read_internal,web_fetch.invoke",
        )

    # ------------------------------------------------------------------

    def test_success_returns_formatted_text(self):
        """HTTP 200 with body → '[WebFetch] ...' header + body."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "body": "<html>hello</html>",
            "truncated": False,
            "error": None,
        }

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            result = tool("https://example.com")

        mock_post.assert_called_once_with(
            "http://fake-gateway:8013/api/web-fetch/v1/fetch",
            json={"url": "https://example.com", "max_bytes": 1048576},
            headers={
                "Authorization": "Bearer tok-web",
                "X-Tenant-Id": "tnt-web",
                "X-User-Id": "usr-web",
                "X-Service-Scopes": (
                    "mcp.read_internal,skill.read_internal,web_fetch.invoke"
                ),
            },
            timeout=30.0,
        )
        assert result.startswith("[WebFetch] https://example.com")
        assert "HTTP 200" in result
        assert "text/html" in result
        assert "<html>hello</html>" in result
        assert "truncated" not in result

    def test_gateway_url_with_api_prefix_is_not_double_prefixed(self):
        """Helm passes a URL with /api/web-fetch/v1; the tool must not duplicate it."""
        from agentscope_ext.web_fetch.tool import WebFetchTool

        tool = WebFetchTool(
            gateway_url="http://fake-gateway:8013/api/web-fetch/v1",
            token="tok-web",
            tenant_id="tnt-web",
            user_id="usr-web",
            service_scopes="web_fetch.invoke",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "body": "ok",
            "truncated": False,
            "error": None,
        }

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            tool("https://example.com")

        assert mock_post.call_args.args[0] == (
            "http://fake-gateway:8013/api/web-fetch/v1/fetch"
        )

    def test_success_with_truncated_flag(self):
        """When truncated=True, appends truncation notice."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://example.com/large",
            "status_code": 200,
            "content_type": "text/plain",
            "body": "lots of text",
            "truncated": True,
            "error": None,
        }

        with patch("httpx.post", return_value=mock_resp):
            result = tool("https://example.com/large")

        assert "[... response truncated at limit ...]" in result

    def test_403_returns_blocked_message(self):
        """HTTP 403 from gateway → '[WebFetch blocked]' message."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"detail": "Domain not in allow-list"}

        with patch("httpx.post", return_value=mock_resp):
            result = tool("https://blocked.example.com")

        assert result.startswith("[WebFetch blocked]")
        assert "blocked.example.com" in result
        assert "Domain not in allow-list" in result

    def test_504_returns_gateway_timeout_message(self):
        """HTTP 504 → gateway-timeout error text."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 504

        with patch("httpx.post", return_value=mock_resp):
            result = tool("https://slow.example.com")

        assert "[WebFetch error]" in result
        assert "timed out" in result.lower() or "Gateway timed out" in result

    def test_connect_error_returns_error_message(self):
        """ConnectError → '[WebFetch error] Could not reach gateway' text."""
        import httpx

        tool = self._make_tool()

        with patch("httpx.post", side_effect=httpx.ConnectError("connection refused")):
            result = tool("https://example.com")

        assert result.startswith("[WebFetch error] Could not reach gateway")
        assert "fake-gateway:8013" in result

    def test_timeout_returns_error_message(self):
        """TimeoutException → '[WebFetch error] ... timed out' text."""
        import httpx

        tool = self._make_tool()

        with patch("httpx.post", side_effect=httpx.TimeoutException("timed out")):
            result = tool("https://example.com")

        assert "[WebFetch error]" in result
        assert "timed out" in result.lower()

    def test_other_4xx_returns_error_code(self):
        """Any other 4xx/5xx → '[WebFetch error] Gateway returned HTTP {code}'."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("httpx.post", return_value=mock_resp):
            result = tool("https://example.com")

        assert "[WebFetch error]" in result
        assert "500" in result

    def test_gateway_error_field_propagated(self):
        """When gateway returns 200 but body contains error field, surface it."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://example.com",
            "status_code": None,
            "content_type": None,
            "body": "",
            "truncated": False,
            "error": "Private IP address blocked",
        }

        with patch("httpx.post", return_value=mock_resp):
            result = tool("https://example.com")

        assert "[WebFetch error]" in result
        assert "Private IP address blocked" in result

    def test_custom_max_bytes_forwarded(self):
        """max_bytes argument is forwarded in the POST payload."""
        tool = self._make_tool()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "body": "ok",
            "truncated": False,
            "error": None,
        }

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            tool("https://example.com", max_bytes=512)

        _, kwargs = mock_post.call_args
        assert kwargs["json"]["max_bytes"] == 512


# ---------------------------------------------------------------------------
# install() / hook-wrapping tests
# ---------------------------------------------------------------------------


class TestWebFetchInstall:
    """Tests for the install() wrapper that injects WebFetchTool into the toolkit."""

    @pytest.fixture(autouse=True)
    def _reload_module(self):
        """Ensure the extension module is freshly loaded for each test so
        the _INSTALLED guard doesn't bleed between tests.  The conftest
        autouse fixture already restores hooks after each test."""
        mod = sys.modules.get("agentscope_ext.web_fetch")
        if mod is not None:
            mod._INSTALLED = False
        yield
        mod = sys.modules.get("agentscope_ext.web_fetch")
        if mod is not None:
            mod._INSTALLED = False

    def test_install_appends_tool_when_url_set(self):
        """install() wraps build_toolkit and appends WebFetchTool when env has URL."""
        import agentscope_ext.runtime_worker.hooks as hooks_mod
        from agentscope_ext.web_fetch.tool import WebFetchTool

        original_build = hooks_mod.build_toolkit

        # Replace build_toolkit with one that returns a known fake toolkit.
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.web_fetch as wf_mod

        wf_mod.install()

        env = {
            "WEB_FETCH_GATEWAY_URL": "http://fake-gw:8013",
            "FOUNDATION_API_TOKEN": "tok-web",
            "FOUNDATION_USER_ID": "usr-web",
            "FOUNDATION_SERVICE_SCOPES": "web_fetch.invoke",
        }
        result = hooks_mod.build_toolkit({}, "t1", env)

        assert result is toolkit
        assert len(toolkit.tools) == 1
        assert isinstance(toolkit.tools[0], WebFetchTool)

        # Restore
        hooks_mod.build_toolkit = original_build

    def test_install_skips_when_url_missing(self, caplog):
        """install() logs a warning and does not append any tool when URL absent."""
        import logging

        import agentscope_ext.runtime_worker.hooks as hooks_mod

        original_build = hooks_mod.build_toolkit
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.web_fetch as wf_mod

        with caplog.at_level(logging.WARNING, logger="agentscope_ext.web_fetch"):
            wf_mod.install()
            hooks_mod.build_toolkit({}, "t1", {})

        assert len(toolkit.tools) == 0
        assert "WEB_FETCH_GATEWAY_URL" in caplog.text

        hooks_mod.build_toolkit = original_build

    def test_install_is_idempotent(self):
        """Calling install() twice wraps build_toolkit only once."""
        import agentscope_ext.runtime_worker.hooks as hooks_mod
        from agentscope_ext.web_fetch.tool import WebFetchTool

        original_build = hooks_mod.build_toolkit
        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit

        import agentscope_ext.web_fetch as wf_mod

        wf_mod.install()
        wf_mod.install()  # second call — must be a no-op

        env = {"WEB_FETCH_GATEWAY_URL": "http://fake-gw:8013"}
        hooks_mod.build_toolkit({}, "t1", env)

        # Tool registered exactly once, not twice.
        assert len(toolkit.tools) == 1
        assert isinstance(toolkit.tools[0], WebFetchTool)

        hooks_mod.build_toolkit = original_build

    def test_install_does_not_crash_on_broken_toolkit(self, caplog):
        """If toolkit.tool_groups[0] is unavailable, install() logs and returns."""
        import logging

        import agentscope_ext.runtime_worker.hooks as hooks_mod

        original_build = hooks_mod.build_toolkit

        class _BrokenToolkit:
            tool_groups: list = []  # empty → IndexError

        hooks_mod.build_toolkit = lambda task, tid, env: _BrokenToolkit()

        import agentscope_ext.web_fetch as wf_mod

        wf_mod.install()

        env = {"WEB_FETCH_GATEWAY_URL": "http://fake-gw:8013"}
        with caplog.at_level(logging.WARNING, logger="agentscope_ext.web_fetch"):
            result = hooks_mod.build_toolkit({}, "t1", env)

        # Must not raise; result is the broken toolkit.
        assert isinstance(result, _BrokenToolkit)

        hooks_mod.build_toolkit = original_build
