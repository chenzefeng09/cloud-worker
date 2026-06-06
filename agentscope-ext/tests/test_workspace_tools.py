"""Tests for ``agentscope_ext.runtime_worker._workspace_tools``.

Covers:
- ``register_into`` appends Read/Grep/Glob/Write/Edit/Bash instances to
  tool_groups[0].tools
- ``register_into`` skips gracefully when a class is unavailable
- ``register_into`` does not raise when toolkit.tool_groups is absent
- ``build_toolkit`` returns a toolkit containing the 6 built-in tools
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Fake AgentScope stubs
# ---------------------------------------------------------------------------


class _FakeToolGroup:
    """Minimal ToolGroup stub."""

    def __init__(self):
        self.name = "basic"
        self.tools: list = []
        self.mcps: list = []


class _FakeToolkit:
    """Toolkit stub that exposes tool_groups[0].tools."""

    def __init__(self):
        self.tool_groups = [_FakeToolGroup()]

    @property
    def tools(self):
        return self.tool_groups[0].tools


class _FakeRead:
    """Stub for agentscope.tool.Read."""

    pass


class _FakeGrep:
    """Stub for agentscope.tool.Grep."""

    pass


class _FakeGlob:
    """Stub for agentscope.tool.Glob."""

    pass


class _FakeWrite:
    """Stub for agentscope.tool.Write."""

    pass


class _FakeEdit:
    """Stub for agentscope.tool.Edit."""

    pass


class _FakeBash:
    """Stub for agentscope.tool.Bash."""

    pass


@pytest.fixture
def fake_agentscope(monkeypatch):
    """Inject a minimal ``agentscope.tool`` stub so tests don't need real
    agentscope installed (same pattern as the rest of the test suite)."""

    tool_mod = types.ModuleType("agentscope.tool")
    tool_mod.Toolkit = _FakeToolkit
    tool_mod.Read = _FakeRead
    tool_mod.Grep = _FakeGrep
    tool_mod.Glob = _FakeGlob
    tool_mod.Write = _FakeWrite
    tool_mod.Edit = _FakeEdit
    tool_mod.Bash = _FakeBash

    monkeypatch.setitem(sys.modules, "agentscope.tool", tool_mod)

    as_mod = sys.modules.get("agentscope") or types.ModuleType("agentscope")
    monkeypatch.setitem(sys.modules, "agentscope", as_mod)

    return tool_mod


# ---------------------------------------------------------------------------
# register_into tests
# ---------------------------------------------------------------------------


def test_register_into_appends_six_tools(monkeypatch, fake_agentscope):
    """register_into appends Read, Grep, Glob, Write, Edit, Bash instances."""
    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(wt)

    toolkit = _FakeToolkit()
    wt.register_into(toolkit)

    assert len(toolkit.tools) == 6
    types_registered = [type(t) for t in toolkit.tools]
    assert _FakeRead in types_registered
    assert _FakeGrep in types_registered
    assert _FakeGlob in types_registered
    assert _FakeWrite in types_registered
    assert _FakeEdit in types_registered
    assert _FakeBash in types_registered


def test_register_into_skips_missing_class(monkeypatch, fake_agentscope):
    """If one class is missing from agentscope.tool, others are still registered."""
    # Remove Grep from the stub.
    del fake_agentscope.Grep

    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(wt)

    toolkit = _FakeToolkit()
    wt.register_into(toolkit)  # must not raise

    # Read, Glob, Write, Edit, Bash should still be registered (5 tools).
    assert len(toolkit.tools) == 5
    types_registered = [type(t) for t in toolkit.tools]
    assert _FakeRead in types_registered
    assert _FakeGlob in types_registered
    assert _FakeWrite in types_registered
    assert _FakeEdit in types_registered
    assert _FakeBash in types_registered


def test_register_into_handles_no_tool_groups(monkeypatch, fake_agentscope):
    """A toolkit without tool_groups does not crash register_into."""

    class _BrokenToolkit:
        # No tool_groups attribute at all.
        pass

    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(wt)

    toolkit = _BrokenToolkit()
    wt.register_into(toolkit)  # must not raise


def test_register_into_handles_empty_tool_groups(monkeypatch, fake_agentscope):
    """A toolkit with an empty tool_groups list does not crash register_into."""

    class _EmptyGroupsToolkit:
        tool_groups: list = []

    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(wt)

    toolkit = _EmptyGroupsToolkit()
    wt.register_into(toolkit)  # must not raise


def test_register_into_skips_all_when_agentscope_not_installed(
    monkeypatch, fake_agentscope
):
    """If agentscope.tool itself is not importable, register_into logs and skips."""
    # Simulate agentscope not installed by replacing the module with an ImportError stub.
    monkeypatch.delitem(sys.modules, "agentscope.tool")

    # Also make the import itself fail.
    import builtins

    real_import = builtins.__import__

    def _failing_import(name, *args, **kwargs):
        if name == "agentscope.tool":
            raise ImportError("agentscope not installed (test stub)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _failing_import)

    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(wt)

    toolkit = _FakeToolkit()
    wt.register_into(toolkit)  # must not raise

    # Nothing was registered.
    assert len(toolkit.tools) == 0


# ---------------------------------------------------------------------------
# build_toolkit integration
# ---------------------------------------------------------------------------


def test_build_toolkit_contains_builtin_tools(monkeypatch, fake_agentscope):
    """build_toolkit() must return a toolkit with all 6 built-in tools registered."""
    from agentscope_ext.runtime_worker import hooks as hooks_mod

    importlib.reload(hooks_mod)

    toolkit = hooks_mod.build_toolkit({}, "tnt-test", {})

    assert isinstance(toolkit, _FakeToolkit)
    assert len(toolkit.tools) == 6

    types_registered = [type(t) for t in toolkit.tools]
    assert _FakeRead in types_registered, f"Read not found in {types_registered}"
    assert _FakeGrep in types_registered, f"Grep not found in {types_registered}"
    assert _FakeGlob in types_registered, f"Glob not found in {types_registered}"
    assert _FakeWrite in types_registered, f"Write not found in {types_registered}"
    assert _FakeEdit in types_registered, f"Edit not found in {types_registered}"
    assert _FakeBash in types_registered, f"Bash not found in {types_registered}"


def test_build_toolkit_does_not_raise_if_registration_fails(
    monkeypatch, fake_agentscope
):
    """A broken toolkit must not crash build_toolkit(); it logs and continues."""

    class _BrokenToolkit:
        tool_groups = []  # empty → IndexError in register_into

    fake_agentscope.Toolkit = _BrokenToolkit

    from agentscope_ext.runtime_worker import hooks as hooks_mod
    from agentscope_ext.runtime_worker import _workspace_tools as wt

    importlib.reload(hooks_mod)
    importlib.reload(wt)

    # Should not raise.
    toolkit = _BrokenToolkit()
    wt.register_into(toolkit)  # must not raise
