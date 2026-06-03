"""Force every LLM call through ``foundation.llm-gateway``.

Replaces ``hooks.build_model`` so that the agent talks to the gateway
instead of holding raw provider keys.  ``hooks.build_formatter`` is
also overridden — the gateway speaks OpenAI's wire format by default,
but Anthropic-native message shape is required for Claude tool-use
flows, so we keep the formatter choice driven by ``LLM_PROVIDER``.

Activation: call :func:`install` exactly once; idempotent.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

from . import model as _model

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _hooks.build_model = _build_gateway_model
    _hooks.build_formatter = _build_gateway_formatter


def _build_gateway_model(task: dict, env: Mapping[str, str]) -> Any:
    return _model.make_gateway_model(task=task, env=env)


def _build_gateway_formatter(task: dict, env: Mapping[str, str]) -> Any:
    """Pick the formatter that matches the *agent-side* message shape.

    The gateway accepts both shapes and converts them internally; on
    the agent side we still need the formatter to match the model
    family the agent thinks it's calling so tool-use blocks round-trip
    correctly.
    """
    del task
    provider = env.get("LLM_PROVIDER", "openai").lower()
    if provider == "anthropic":
        from agentscope.formatter import AnthropicChatFormatter

        return AnthropicChatFormatter()
    from agentscope.formatter import OpenAIChatFormatter

    return OpenAIChatFormatter()


__all__ = ["install"]
