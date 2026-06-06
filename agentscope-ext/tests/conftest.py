"""Shared test fixtures.

The four sibling-extension test files each install ``hooks`` wrappers
on module globals — without an autouse teardown, those wrappers leak
to the next test file and break ``test_runtime_worker`` (which assumes
pristine defaults and would otherwise try real network calls).
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _restore_hooks():
    """Snapshot ``hooks`` module attributes before each test and restore
    them after, so wrapper installs cannot leak across tests.

    Also resets the ``_INSTALLED`` sentinel on each extension package
    so install() can run cleanly in the next test.
    """
    import agentscope_ext.runtime_worker.hooks as hooks_mod

    snapshot = {
        "build_model": hooks_mod.build_model,
        "build_formatter": hooks_mod.build_formatter,
        "build_toolkit": hooks_mod.build_toolkit,
        "attach_hooks": hooks_mod.attach_hooks,
    }
    try:
        yield
    finally:
        for k, v in snapshot.items():
            setattr(hooks_mod, k, v)
        for name in (
            "agentscope_ext.event_audit_sink",
            "agentscope_ext.llm_gateway_enforcer",
            "agentscope_ext.mcp_tenant_injector",
            "agentscope_ext.skills_loader",
            "agentscope_ext.web_fetch",
        ):
            mod = sys.modules.get(name)
            if mod is not None and hasattr(mod, "_INSTALLED"):
                mod._INSTALLED = False
