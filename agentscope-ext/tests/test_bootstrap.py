"""Tests for ``agentscope_ext.bootstrap`` env-driven activation."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


@pytest.fixture
def fresh_modules(monkeypatch):
    """Reload everything so each test sees clean install() sentinels.

    On teardown the original ``hooks`` callables are restored so the
    next test file (e.g. ``test_runtime_worker``) sees a pristine
    module — without this, monkey-patched wrappers would leak across
    test files and trigger real network calls.
    """
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)
    original = {
        "build_model": hooks_mod.build_model,
        "build_formatter": hooks_mod.build_formatter,
        "build_toolkit": hooks_mod.build_toolkit,
        "attach_hooks": hooks_mod.attach_hooks,
    }

    formatter_mod = types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "anth"
    formatter_mod.OpenAIChatFormatter = lambda: "oai"
    monkeypatch.setitem(sys.modules, "agentscope.formatter", formatter_mod)
    monkeypatch.setitem(sys.modules, "agentscope.model", types.ModuleType("agentscope.model"))

    class _Toolkit:
        def __init__(self):
            self.tools = []
            self.servers = []

        def register_tool_function(self, fn, **_):
            self.tools.append(fn)

        def register_mcp_server(self, name, **kwargs):
            self.servers.append((name, kwargs))

    tool_mod = types.ModuleType("agentscope.tool")
    tool_mod.Toolkit = _Toolkit
    monkeypatch.setitem(sys.modules, "agentscope.tool", tool_mod)

    for name in (
        "agentscope_ext.event_audit_sink",
        "agentscope_ext.llm_gateway_enforcer",
        "agentscope_ext.mcp_tenant_injector",
        "agentscope_ext.skills_loader",
    ):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
            mod = sys.modules[name]
            if hasattr(mod, "_INSTALLED"):
                mod._INSTALLED = False

    try:
        yield hooks_mod
    finally:
        for k, v in original.items():
            setattr(hooks_mod, k, v)
        for name in (
            "agentscope_ext.event_audit_sink",
            "agentscope_ext.llm_gateway_enforcer",
            "agentscope_ext.mcp_tenant_injector",
            "agentscope_ext.skills_loader",
        ):
            mod = sys.modules.get(name)
            if mod is not None and hasattr(mod, "_INSTALLED"):
                mod._INSTALLED = False


def test_enable_from_env_installs_named_extensions(fresh_modules, monkeypatch):
    hooks_mod = fresh_modules
    original_attach = hooks_mod.attach_hooks
    original_model = hooks_mod.build_model

    from agentscope_ext import bootstrap

    installed = bootstrap.enable_from_env(
        "event_audit_sink,llm_gateway_enforcer",
    )
    assert installed == ["event_audit_sink", "llm_gateway_enforcer"]
    assert hooks_mod.attach_hooks is not original_attach
    assert hooks_mod.build_model is not original_model


def test_enable_from_env_skips_unknown_names(fresh_modules, caplog):
    from agentscope_ext import bootstrap

    with caplog.at_level("WARNING"):
        installed = bootstrap.enable_from_env("event_audit_sink,not_a_thing")
    assert installed == ["event_audit_sink"]
    assert any("unknown extension" in r.message for r in caplog.records)


def test_enable_from_env_is_idempotent(fresh_modules):
    hooks_mod = fresh_modules
    from agentscope_ext import bootstrap

    bootstrap.enable_from_env("mcp_tenant_injector,skills_loader")
    wrapped = hooks_mod.build_toolkit
    bootstrap.enable_from_env("mcp_tenant_injector,skills_loader")
    assert hooks_mod.build_toolkit is wrapped, "second pass must not re-wrap"


def test_enable_all_runs_in_order(fresh_modules):
    hooks_mod = fresh_modules
    original_attach = hooks_mod.attach_hooks
    original_model = hooks_mod.build_model
    original_toolkit = hooks_mod.build_toolkit

    from agentscope_ext import enable_all

    enable_all()
    assert hooks_mod.attach_hooks is not original_attach
    assert hooks_mod.build_model is not original_model
    assert hooks_mod.build_toolkit is not original_toolkit
