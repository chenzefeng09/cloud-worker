"""Tests for ``agentscope_ext.event_audit_sink``."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import httpx
import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


@pytest.fixture
def fresh_hooks(monkeypatch):
    """Reload hooks + the extension so install() starts from a clean slate."""
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)

    import agentscope_ext.event_audit_sink as ext
    importlib.reload(ext)
    # Reset install() sentinel so the test can call install() itself.
    ext._INSTALLED = False
    return hooks_mod, ext


class _RecordingAgent:
    def __init__(self):
        self.hooks: dict[str, dict] = {}

    def register_instance_hook(self, *, hook_type, hook_name, hook):
        self.hooks.setdefault(hook_type, {})[hook_name] = hook


class _Msg:
    def __init__(self, content):
        self.content = content
        self.name = "Worker"
        self.role = "assistant"

    def to_dict(self):
        return {"name": self.name, "content": self.content, "role": self.role}


def _env():
    return {
        "FOUNDATION_API_URL": "https://foundation.local",
        "FOUNDATION_API_TOKEN": "tok-xyz",
        "FOUNDATION_TENANT_ID": "tnt-abc",
        "FOUNDATION_USER_ID": "usr-123",
    }


def test_install_is_idempotent_and_wraps_previous(fresh_hooks):
    hooks_mod, ext = fresh_hooks
    calls: list[str] = []

    def _previous(agent, *, events_path, env):
        calls.append("previous")

    hooks_mod.attach_hooks = _previous
    ext.install()
    ext.install()  # second call must not double-wrap

    agent = _RecordingAgent()
    hooks_mod.attach_hooks(agent, events_path=Path("/tmp/x"), env=_env())
    assert calls == ["previous"], "previous attach_hooks must be called exactly once"
    assert "event_audit_sink_post" in agent.hooks["post_print"]


def test_post_succeeds_and_carries_expected_body(fresh_hooks, monkeypatch):
    hooks_mod, ext = fresh_hooks
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    from agentscope_ext.event_audit_sink import sink

    sink.set_client(httpx.Client(transport=transport))

    ext.install()
    agent = _RecordingAgent()
    hooks_mod.attach_hooks(agent, events_path=Path("/tmp/y"), env=_env())

    hook = agent.hooks["post_print"]["event_audit_sink_post"]
    hook(agent, {"msg": _Msg("hello world")})

    assert captured["url"] == "https://foundation.local/audit/v1/events"
    assert captured["auth"] == "Bearer tok-xyz"
    assert captured["body"]["tenant_id"] == "tnt-abc"
    assert captured["body"]["user_id"] == "usr-123"
    assert captured["body"]["event_type"] == "agent_msg"
    assert captured["body"]["resource"] == "agent_session"
    assert captured["body"]["payload"]["content"] == "hello world"

    sink.set_client(None)


def test_post_failure_does_not_break_agent(fresh_hooks, caplog):
    hooks_mod, ext = fresh_hooks

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(_handler)
    from agentscope_ext.event_audit_sink import sink

    sink.set_client(httpx.Client(transport=transport))

    ext.install()
    agent = _RecordingAgent()
    hooks_mod.attach_hooks(agent, events_path=Path("/tmp/z"), env=_env())

    hook = agent.hooks["post_print"]["event_audit_sink_post"]
    # Must NOT raise even though the upstream returned 500.
    with caplog.at_level("WARNING"):
        hook(agent, {"msg": _Msg("danger")})

    assert any("POST failed" in r.message for r in caplog.records)

    sink.set_client(None)


def test_missing_msg_is_noop(fresh_hooks):
    hooks_mod, ext = fresh_hooks
    ext.install()
    agent = _RecordingAgent()
    hooks_mod.attach_hooks(agent, events_path=Path("/tmp/m"), env=_env())

    hook = agent.hooks["post_print"]["event_audit_sink_post"]
    # No HTTP traffic should fly; if it did the default client would
    # fail open without a mock transport configured.
    hook(agent, {})  # no 'msg' key, must just return
