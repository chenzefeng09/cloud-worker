"""Tests for ``agentscope_ext.llm_gateway_enforcer``."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


@pytest.fixture
def fresh(monkeypatch):
    """Reload hooks + extension; install fake formatter module so
    build_formatter doesn't try to import the real AgentScope."""
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)

    formatter_mod = types.ModuleType("agentscope.formatter")
    formatter_mod.AnthropicChatFormatter = lambda: "fake-anthropic-formatter"
    formatter_mod.OpenAIChatFormatter = lambda: "fake-openai-formatter"
    monkeypatch.setitem(sys.modules, "agentscope.formatter", formatter_mod)
    # Make sure no agentscope.model sneaks in — we want make_gateway_model
    # to fall through to the plain callable for these tests.
    monkeypatch.setitem(sys.modules, "agentscope.model", types.ModuleType("agentscope.model"))

    import agentscope_ext.llm_gateway_enforcer as ext
    importlib.reload(ext)
    ext._INSTALLED = False
    return hooks_mod, ext


def _env():
    return {
        "FOUNDATION_API_URL": "https://foundation.local",
        "FOUNDATION_API_TOKEN": "tok-xyz",
        "FOUNDATION_TENANT_ID": "tnt-abc",
        "FOUNDATION_USER_ID": "usr-123",
        "LLM_MODEL": "gpt-4o-mini",
        "LLM_PROVIDER": "openai",
    }


def test_install_overrides_model_and_formatter(fresh):
    hooks_mod, ext = fresh
    original_model = hooks_mod.build_model
    original_fmt = hooks_mod.build_formatter
    ext.install()
    ext.install()  # idempotent
    assert hooks_mod.build_model is not original_model
    assert hooks_mod.build_formatter is not original_fmt


def test_gateway_call_hits_expected_endpoint_with_metadata(fresh):
    hooks_mod, ext = fresh
    captured = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "hi back",
                            "tool_calls": [],
                        }
                    }
                ]
            },
        )

    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    model_mod.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))

    ext.install()
    model = hooks_mod.build_model({}, _env())

    reply = asyncio.run(model([{"role": "user", "content": "hello"}]))

    assert captured["url"] == "https://foundation.local/llm-gateway/v1/chat/completions"
    assert captured["auth"] == "Bearer tok-xyz"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["metadata"] == {
        "tenant_id": "tnt-abc",
        "user_id": "usr-123",
    }
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert reply.content == "hi back"
    assert reply.tool_calls == []

    model_mod.set_client(None)


def test_formatter_follows_provider(fresh):
    hooks_mod, ext = fresh
    ext.install()
    env = _env()
    env["LLM_PROVIDER"] = "anthropic"
    fmt = hooks_mod.build_formatter({}, env)
    assert fmt == "fake-anthropic-formatter"

    env["LLM_PROVIDER"] = "openai"
    fmt = hooks_mod.build_formatter({}, env)
    assert fmt == "fake-openai-formatter"


def test_tool_calls_pass_through(fresh):
    hooks_mod, ext = fresh

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": "{\"q\":\"agentscope\"}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    model_mod.set_client(httpx.Client(transport=httpx.MockTransport(_handler)))
    ext.install()
    model = hooks_mod.build_model({}, _env())
    reply = asyncio.run(model([{"role": "user", "content": "search"}]))
    assert reply.content == ""
    assert len(reply.tool_calls) == 1
    assert reply.tool_calls[0]["function"]["name"] == "search"

    model_mod.set_client(None)


def test_missing_base_url_raises_during_build(fresh):
    hooks_mod, ext = fresh
    ext.install()
    env = _env()
    env.pop("FOUNDATION_API_URL")
    with pytest.raises(ValueError):
        hooks_mod.build_model({}, env)
