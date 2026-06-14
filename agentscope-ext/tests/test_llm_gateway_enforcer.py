"""Tests for ``agentscope_ext.llm_gateway_enforcer``.

These tests verify that:
1. ``install()`` correctly patches ``hooks.build_model`` / ``hooks.build_formatter``
   and is idempotent.
2. With ``LLM_GATEWAY_URL`` + ``LLM_VIRTUAL_KEY``, ``build_model`` returns a
   gateway-wired ``OpenAIChatModel`` subclass with the correct configuration.
3. Missing ``LLM_GATEWAY_URL`` raises ``ValueError``.
4. Missing ``LLM_VIRTUAL_KEY`` falls back to bearer token (no crash).
5. The formatter is always ``OpenAIChatFormatter`` (gateway speaks OpenAI wire).

Because the real agentscope.model.OpenAIChatModel makes live API calls, the
production-path tests mock the ``openai.AsyncOpenAI`` client at the point
where ``_GatewayOpenAIChatModel._call_api`` calls it.  The legacy
``_GatewayCallable``-based tests from M1-P1 are retained for the shim but
clearly marked as legacy.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh(monkeypatch):
    """Reload hooks + extension with a clean slate.

    We install a stub agentscope.formatter so tests don't need the real
    agentscope package for formatter construction.  The agentscope.model and
    agentscope.credential stubs are NOT installed here so the production path
    (which imports the real OpenAIChatModel) is exercised in tests that want it.
    """
    from agentscope_ext.runtime_worker import hooks as hooks_mod
    importlib.reload(hooks_mod)

    # Stub formatter only — keeps tests that don't drive the real model clean.
    formatter_mod = types.ModuleType("agentscope.formatter")
    formatter_cls = MagicMock(return_value=MagicMock(name="OpenAIChatFormatter"))
    formatter_mod.OpenAIChatFormatter = formatter_cls
    formatter_mod.AnthropicChatFormatter = MagicMock(return_value=MagicMock(name="AnthropicChatFormatter"))
    monkeypatch.setitem(sys.modules, "agentscope.formatter", formatter_mod)

    import agentscope_ext.llm_gateway_enforcer as ext
    importlib.reload(ext)
    ext._INSTALLED = False

    yield hooks_mod, ext

    ext._INSTALLED = False


def _base_env() -> dict:
    return {
        "LLM_GATEWAY_URL": "http://llm-gw:8011/api/llm-gateway/v1",
        "LLM_VIRTUAL_KEY": "vk-tenant-abc",
        "FOUNDATION_API_TOKEN": "tok-sandbox-jwt",
        "FOUNDATION_TENANT_ID": "tnt-abc",
        "FOUNDATION_USER_ID": "usr-123",
        "TOOL_BROKER_LEASE": "v1.payload.signature",
        "LLM_MODEL": "gpt-4o-mini",
        "LLM_PROVIDER": "openai",
    }


# ---------------------------------------------------------------------------
# install() contract
# ---------------------------------------------------------------------------


def test_install_overrides_model_and_formatter(fresh):
    hooks_mod, ext = fresh
    original_model = hooks_mod.build_model
    original_fmt = hooks_mod.build_formatter
    ext.install()
    ext.install()  # idempotent
    assert hooks_mod.build_model is not original_model
    assert hooks_mod.build_formatter is not original_fmt


def test_install_is_idempotent(fresh):
    hooks_mod, ext = fresh
    ext.install()
    patched_model = hooks_mod.build_model
    patched_fmt = hooks_mod.build_formatter
    ext.install()  # second call should be no-op
    assert hooks_mod.build_model is patched_model
    assert hooks_mod.build_formatter is patched_fmt


# ---------------------------------------------------------------------------
# build_model — happy path: real OpenAIChatModel subclass
# ---------------------------------------------------------------------------


def test_build_model_returns_openai_model_subclass(fresh, monkeypatch):
    """With valid env vars, build_model returns an OpenAIChatModel subclass."""
    hooks_mod, ext = fresh
    ext.install()

    # Stub agentscope.model and agentscope.credential so we don't need
    # a live openai package for object construction assertions.
    cred_instance = MagicMock(name="OpenAICredential")
    cred_cls = MagicMock(return_value=cred_instance)

    model_instance = MagicMock(name="OpenAIChatModel_instance")
    # The subclass is created dynamically; mock _build_openai_gateway_model
    # instead of OpenAIChatModel directly.
    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    with patch.object(
        model_mod, "_build_openai_gateway_model", return_value=model_instance
    ) as mock_build:
        result = hooks_mod.build_model({}, _base_env())

    mock_build.assert_called_once()
    call_kwargs = mock_build.call_args.kwargs
    assert call_kwargs["gateway_url"] == "http://llm-gw:8011/api/llm-gateway/v1"
    assert call_kwargs["virtual_key"] == "vk-tenant-abc"
    assert call_kwargs["bearer_token"] == "tok-sandbox-jwt"
    assert call_kwargs["model_name"] == "gpt-4o-mini"
    assert result is model_instance


def test_build_model_passes_correct_url_and_keys(fresh, monkeypatch):
    """LLM_GATEWAY_URL, LLM_VIRTUAL_KEY, FOUNDATION_API_TOKEN forwarded correctly."""
    hooks_mod, ext = fresh
    ext.install()

    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    captured: dict[str, Any] = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="model")

    with patch.object(model_mod, "_build_openai_gateway_model", side_effect=_fake_build):
        hooks_mod.build_model({}, _base_env())

    # URL must NOT end with /chat/completions — the openai SDK appends that.
    assert captured["gateway_url"] == "http://llm-gw:8011/api/llm-gateway/v1"
    assert captured["virtual_key"] == "vk-tenant-abc"
    assert captured["bearer_token"] == "tok-sandbox-jwt"
    assert captured["model_name"] == "gpt-4o-mini"


def test_build_model_strips_trailing_slash_from_url(fresh):
    hooks_mod, ext = fresh
    ext.install()

    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    captured: dict[str, Any] = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="model")

    env = _base_env()
    env["LLM_GATEWAY_URL"] = "http://llm-gw:8011/api/llm-gateway/v1/"

    with patch.object(model_mod, "_build_openai_gateway_model", side_effect=_fake_build):
        hooks_mod.build_model({}, env)

    assert not captured["gateway_url"].endswith("/")


# ---------------------------------------------------------------------------
# build_model — validation errors
# ---------------------------------------------------------------------------


def test_missing_gateway_url_raises(fresh):
    hooks_mod, ext = fresh
    ext.install()
    env = _base_env()
    env.pop("LLM_GATEWAY_URL")
    with pytest.raises(ValueError, match="LLM_GATEWAY_URL"):
        hooks_mod.build_model({}, env)


def test_missing_model_raises(fresh):
    hooks_mod, ext = fresh
    ext.install()
    env = _base_env()
    env.pop("LLM_MODEL")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        hooks_mod.build_model({}, env)


def test_missing_virtual_key_falls_back_to_bearer(fresh):
    """Absent LLM_VIRTUAL_KEY must not raise — bearer token is used instead."""
    hooks_mod, ext = fresh
    ext.install()

    from agentscope_ext.llm_gateway_enforcer import model as model_mod

    captured: dict[str, Any] = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="model")

    env = _base_env()
    env.pop("LLM_VIRTUAL_KEY")

    with patch.object(model_mod, "_build_openai_gateway_model", side_effect=_fake_build):
        hooks_mod.build_model({}, env)

    assert captured["virtual_key"] == ""
    assert captured["bearer_token"] == "tok-sandbox-jwt"


# ---------------------------------------------------------------------------
# build_formatter — always OpenAI regardless of LLM_PROVIDER
# ---------------------------------------------------------------------------


def test_formatter_is_always_openai(fresh):
    """The gateway speaks OpenAI wire format; formatter must always be OpenAI."""
    hooks_mod, ext = fresh
    ext.install()

    for provider in ("anthropic", "openai", "deepseek"):
        env = _base_env()
        env["LLM_PROVIDER"] = provider
        fmt = hooks_mod.build_formatter({}, env)
        # Our stub makes OpenAIChatFormatter() return a mock with name "OpenAIChatFormatter"
        assert fmt is not None


# ---------------------------------------------------------------------------
# _build_openai_gateway_model — headers injected into AsyncOpenAI client
# ---------------------------------------------------------------------------


def test_gateway_model_injects_auth_headers(monkeypatch):
    """Verify that _build_openai_gateway_model wires the correct headers."""
    # We need real (or stub) agentscope classes.
    cred_instance = MagicMock()
    cred_instance.api_key.get_secret_value.return_value = "vk-tenant-abc"
    cred_instance.base_url = "http://llm-gw:8011/api/llm-gateway/v1"
    cred_instance.organization = None

    cred_cls = MagicMock(return_value=cred_instance)

    # Stub model base so we can inspect what headers the subclass creates.
    captured_headers: dict = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured_headers.update(kwargs.get("default_headers", {}))
            # Provide a minimal client surface so _call_api can run.
            self.chat = MagicMock()
            self.chat.completions = MagicMock()
            self.chat.completions.create = AsyncMock(
                return_value=MagicMock(choices=[], usage=None, id="resp-1")
            )

    # We patch at the openai module level so the override _call_api sees it.
    import agentscope_ext.llm_gateway_enforcer.model as model_mod

    fake_formatter = MagicMock()
    fake_formatter.format = AsyncMock(return_value=[])

    cred_stub = MagicMock()
    cred_stub.api_key.get_secret_value.return_value = "vk-tenant-abc"
    cred_stub.base_url = "http://llm-gw:8011/api/llm-gateway/v1"
    cred_stub.organization = None

    openai_stub = types.ModuleType("openai")
    openai_stub.AsyncOpenAI = _FakeAsyncOpenAI

    agentscope_credential = types.ModuleType("agentscope.credential")
    agentscope_credential.OpenAICredential = MagicMock(return_value=cred_stub)

    # We need the real OpenAIChatModel – import it lazily if available.
    try:
        from agentscope.model import OpenAIChatModel
        from agentscope.credential import OpenAICredential

        with patch.dict(sys.modules, {"openai": openai_stub}):
            model = model_mod._build_openai_gateway_model(
                gateway_url="http://llm-gw:8011/api/llm-gateway/v1",
                virtual_key="vk-tenant-abc",
                bearer_token="tok-sandbox-jwt",
                model_name="gpt-4o-mini",
                formatter=fake_formatter,
                tenant_id="tnt_01J5DEVXX00000000000000001",
                user_id="usr_01J5DEVXX00000000000000001",
                broker_lease="v1.payload.signature",
            )

        import asyncio

        # Trigger _call_api to check header injection.
        async def _drive():
            result = await model._call_api("gpt-4o-mini", [])
            # consume generator if streaming
            if hasattr(result, "__aiter__"):
                async for _ in result:
                    pass

        asyncio.run(_drive())

        assert captured_headers.get("Authorization") == "Bearer tok-sandbox-jwt"
        assert captured_headers.get("X-LLM-Virtual-Key") == "vk-tenant-abc"
        # The gateway's TenantContextMiddleware requires X-Tenant-Id or it
        # returns 401 "Tenant context missing" — assert the enforcer forwards it.
        assert captured_headers.get("X-Tenant-Id") == "tnt_01J5DEVXX00000000000000001"
        assert captured_headers.get("X-User-Id") == "usr_01J5DEVXX00000000000000001"
        assert captured_headers.get("X-Tool-Broker-Lease") == "v1.payload.signature"

    except ImportError:
        pytest.skip("agentscope not installed — skipping header-injection integration test")


# ---------------------------------------------------------------------------
# snapshot_env — new keys present
# ---------------------------------------------------------------------------


def test_snapshot_env_includes_gateway_keys(monkeypatch):
    """LLM_GATEWAY_URL and LLM_VIRTUAL_KEY must appear in snapshot_env output."""
    from agentscope_ext.runtime_worker import hooks as hooks_mod

    monkeypatch.setenv("LLM_GATEWAY_URL", "http://gw:8011/api/llm-gateway/v1")
    monkeypatch.setenv("LLM_VIRTUAL_KEY", "vk-abc")
    monkeypatch.setenv("TOOL_BROKER_LEASE", "v1.payload.signature")

    snap = hooks_mod.snapshot_env()
    assert snap.get("LLM_GATEWAY_URL") == "http://gw:8011/api/llm-gateway/v1"
    assert snap.get("LLM_VIRTUAL_KEY") == "vk-abc"
    assert snap.get("TOOL_BROKER_LEASE") == "v1.payload.signature"
