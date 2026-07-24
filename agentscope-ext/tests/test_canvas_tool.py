"""Tests for ``agentscope_ext.canvas_tool`` (canvas v2, Phase B).

Covers:
- ``CreateCanvasTool`` records the payload via the run-state client and returns
  a confirmation string that names the canvas.
- The tool writes a ``/workspace/canvas.json`` mirror (accumulating).
- The tool never crashes when the state client raises (fail-open).
- ``install()`` wraps ``hooks.build_toolkit`` and appends the tool; idempotent.
- ``bootstrap._KNOWN`` maps ``canvas_tool`` and ``enable_from_env`` installs it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Toolkit / ToolGroup stubs (same pattern as test_web_fetch.py)
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


class _FakeStateClient:
    """Records push_canvas calls; assigns a monotonically increasing seq."""

    def __init__(self, *, raises: Exception | None = None):
        self.calls: list[dict] = []
        self._raises = raises

    def push_canvas(self, payload: dict) -> int:
        if self._raises is not None:
            raise self._raises
        self.calls.append(payload)
        return len(self.calls)


# ---------------------------------------------------------------------------
# CreateCanvasTool unit tests
# ---------------------------------------------------------------------------


class TestCreateCanvasTool:
    def _make_tool(self, state_client, tmp_path, monkeypatch):
        from agentscope_ext.canvas_tool import tool as tool_mod

        # Redirect the workspace mirror to a tmp dir.
        monkeypatch.setattr(tool_mod, "_CANVAS_MIRROR", tmp_path / "canvas.json")
        return tool_mod.CreateCanvasTool(state_client=state_client)

    def test_records_payload_and_returns_confirmation(self, tmp_path, monkeypatch):
        sc = _FakeStateClient()
        tool = self._make_tool(sc, tmp_path, monkeypatch)

        result = tool(
            title="AI 与就业",
            content_markdown="# AI 与就业\n\n人工智能正在改变世界。",
            kind="article",
        )

        # Payload written to run-state with the exact wire shape.
        assert sc.calls == [
            {
                "kind": "article",
                "title": "AI 与就业",
                "content_markdown": "# AI 与就业\n\n人工智能正在改变世界。",
                "content_type": "markdown",
            }
        ]
        # Confirmation names the canvas by title (so the model can echo it).
        text = result if isinstance(result, str) else result.content[0].text
        assert "AI 与就业" in text

    def test_records_html_content_type(self, tmp_path, monkeypatch):
        sc = _FakeStateClient()
        tool = self._make_tool(sc, tmp_path, monkeypatch)
        html = "<!doctype html><html><body>hi</body></html>"
        tool(title="打砖块", content_markdown=html, kind="code_note",
             content_type="html")
        assert sc.calls[0]["content_type"] == "html"
        assert sc.calls[0]["content_markdown"] == html

    def test_defaults_kind_to_article(self, tmp_path, monkeypatch):
        sc = _FakeStateClient()
        tool = self._make_tool(sc, tmp_path, monkeypatch)
        tool(title="T", content_markdown="body", kind=None)
        assert sc.calls[0]["kind"] == "article"

    def test_writes_workspace_mirror_accumulating(self, tmp_path, monkeypatch):
        sc = _FakeStateClient()
        tool = self._make_tool(sc, tmp_path, monkeypatch)
        tool(title="A", content_markdown="alpha")
        tool(title="B", content_markdown="beta")

        mirror = json.loads((tmp_path / "canvas.json").read_text(encoding="utf-8"))
        assert [m["title"] for m in mirror] == ["A", "B"]

    def test_failopen_when_state_client_raises(self, tmp_path, monkeypatch):
        sc = _FakeStateClient(raises=RuntimeError("redis down"))
        tool = self._make_tool(sc, tmp_path, monkeypatch)
        # Must not raise; confirmation still returned; mirror still written.
        result = tool(title="Resilient", content_markdown="still works")
        text = result if isinstance(result, str) else result.content[0].text
        assert "Resilient" in text
        mirror = json.loads((tmp_path / "canvas.json").read_text(encoding="utf-8"))
        assert mirror[0]["title"] == "Resilient"


class TestCreateCanvasToolSchema:
    """The description/schema the LLM sees must force prose creation through
    this tool (deepseek only calls tools under strong wording)."""

    def test_description_carries_forcing_language(self):
        from agentscope_ext.canvas_tool.tool import CreateCanvasTool

        desc = CreateCanvasTool.description
        # Mandatory-for-all-prose framing.
        assert "REQUIRED" in desc
        assert "MUST" in desc
        assert "FIRST" in desc
        # Full body goes into the tool, not the reply.
        assert "content_markdown" in desc
        assert "FULL" in desc
        # One-sentence reply / no-repeat contract.
        assert "ONE short" in desc
        # Titled / book-title assignments are explicitly covered.
        assert "《书名号》" in desc

    def test_schema_field_descriptions_are_clear(self):
        from agentscope_ext.canvas_tool.tool import CreateCanvasTool

        props = CreateCanvasTool.input_schema["properties"]
        # kind enum semantics stay intact.
        assert props["kind"]["enum"] == [
            "article",
            "brief",
            "email",
            "report",
            "proposal",
            "social_post",
            "code_note",
        ]
        # title description tells the model to extract from the assignment/《》.
        assert "《书名号》" in props["title"]["description"]
        # content_markdown must be the COMPLETE finished piece and cover the
        # executable-HTML path.
        assert "COMPLETE" in props["content_markdown"]["description"]
        assert "self-contained HTML" in props["content_markdown"]["description"]
        # content_type gates the executable-canvas branch (markdown | html).
        assert props["content_type"]["enum"] == ["markdown", "html"]
        assert props["content_type"]["default"] == "markdown"
        # Required fields unchanged (content_type is optional, defaults markdown).
        assert CreateCanvasTool.input_schema["required"] == [
            "title",
            "content_markdown",
        ]


# ---------------------------------------------------------------------------
# install() / hook-wrapping tests
# ---------------------------------------------------------------------------


class TestCanvasToolInstall:
    @pytest.fixture(autouse=True)
    def _reload_module(self):
        mod = sys.modules.get("agentscope_ext.canvas_tool")
        if mod is not None:
            mod._INSTALLED = False
        yield
        mod = sys.modules.get("agentscope_ext.canvas_tool")
        if mod is not None:
            mod._INSTALLED = False

    def test_install_appends_tool(self, monkeypatch):
        import agentscope_ext.canvas_tool as ct_mod
        import agentscope_ext.runtime_worker.hooks as hooks_mod
        from agentscope_ext.canvas_tool.tool import CreateCanvasTool

        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit
        # from_env must not touch real Redis.
        monkeypatch.setattr(
            "agentscope_ext.runtime_worker.redis_state.from_env",
            lambda: _FakeStateClient(),
        )

        ct_mod.install()
        result = hooks_mod.build_toolkit({}, "t1", {})

        assert result is toolkit
        assert len(toolkit.tools) == 1
        assert isinstance(toolkit.tools[0], CreateCanvasTool)

    def test_install_is_idempotent(self, monkeypatch):
        import agentscope_ext.canvas_tool as ct_mod
        import agentscope_ext.runtime_worker.hooks as hooks_mod

        toolkit = _FakeToolkit()
        hooks_mod.build_toolkit = lambda task, tid, env: toolkit
        monkeypatch.setattr(
            "agentscope_ext.runtime_worker.redis_state.from_env",
            lambda: _FakeStateClient(),
        )

        ct_mod.install()
        ct_mod.install()  # no-op
        hooks_mod.build_toolkit({}, "t1", {})
        assert len(toolkit.tools) == 1

    def test_install_does_not_crash_on_broken_toolkit(self, caplog, monkeypatch):
        import logging

        import agentscope_ext.canvas_tool as ct_mod
        import agentscope_ext.runtime_worker.hooks as hooks_mod

        class _BrokenToolkit:
            tool_groups: list = []  # empty → IndexError

        hooks_mod.build_toolkit = lambda task, tid, env: _BrokenToolkit()
        monkeypatch.setattr(
            "agentscope_ext.runtime_worker.redis_state.from_env",
            lambda: _FakeStateClient(),
        )

        ct_mod.install()
        with caplog.at_level(logging.WARNING, logger="agentscope_ext.canvas_tool"):
            result = hooks_mod.build_toolkit({}, "t1", {})
        assert isinstance(result, _BrokenToolkit)


# ---------------------------------------------------------------------------
# bootstrap wiring
# ---------------------------------------------------------------------------


def test_bootstrap_knows_canvas_tool():
    from agentscope_ext import bootstrap

    assert bootstrap._KNOWN.get("canvas_tool") == "agentscope_ext.canvas_tool"


def test_enable_from_env_installs_canvas_tool(monkeypatch):
    import agentscope_ext.canvas_tool as ct_mod
    from agentscope_ext import bootstrap

    ct_mod._INSTALLED = False
    called = {"n": 0}
    monkeypatch.setattr(ct_mod, "install", lambda: called.__setitem__("n", called["n"] + 1))

    installed = bootstrap.enable_from_env("canvas_tool")
    assert installed == ["canvas_tool"]
    assert called["n"] == 1


# ---------------------------------------------------------------------------
# _canvas_tool_enabled — system-prompt gate (worker)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime_extensions,expected",
    [
        ("mcp_tenant_injector,skills_loader,web_fetch,canvas_tool", True),
        ("canvas_tool", True),
        (" canvas_tool , web_fetch ", True),
        ("mcp_tenant_injector,skills_loader,web_fetch", False),
        ("", False),
    ],
)
def test_canvas_tool_enabled_gate(runtime_extensions, expected):
    """The sys-prompt gate keys off ``canvas_tool`` in RUNTIME_EXTENSIONS."""
    from agentscope_ext.runtime_worker import __main__ as worker

    assert worker._canvas_tool_enabled(
        {"RUNTIME_EXTENSIONS": runtime_extensions}
    ) is expected


def test_canvas_tool_enabled_gate_missing_key():
    from agentscope_ext.runtime_worker import __main__ as worker

    assert worker._canvas_tool_enabled({}) is False
