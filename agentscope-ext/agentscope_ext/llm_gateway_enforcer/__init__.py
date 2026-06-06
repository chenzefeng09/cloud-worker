"""Force every LLM call through ``foundation.llm-gateway``.

Replaces ``hooks.build_model`` and ``hooks.build_formatter`` so that the
agent talks to the gateway via the native agentscope ``OpenAIChatModel``
(subclassed to inject gateway auth headers) instead of holding raw provider
keys.  The gateway exposes an OpenAI-compatible endpoint, so we always use
``OpenAIChatFormatter`` on the agent side regardless of the underlying model
family — the gateway handles provider translation.

Activation
----------
Call :func:`install` exactly once.  The call is idempotent.  In normal
operation ``install()`` is invoked by :mod:`agentscope_ext.bootstrap` when
``RUNTIME_EXTENSIONS`` contains ``llm_gateway_enforcer``.

Env vars consumed (all injected by the scheduler)
-------------------------------------------------
* ``LLM_GATEWAY_URL``      — gateway base URL *with* /api/llm-gateway/v1
* ``LLM_VIRTUAL_KEY``      — tenant virtual key
* ``FOUNDATION_API_TOKEN`` — per-run sandbox JWT (bearer)
* ``LLM_MODEL``            — model routing key

Grayscale / default-off
-----------------------
``llm_gateway_enforcer`` is **not** in the default ``RUNTIME_EXTENSIONS``
value.  It must be explicitly set by the operator/scheduler.  Sandboxes
without it continue to direct-connect to the provider using raw keys.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

from . import model as _model

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    """Install the gateway enforcer hooks.  Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    _hooks.build_model = _build_gateway_model
    _hooks.build_formatter = _build_gateway_formatter


def _build_gateway_model(task: dict, env: Mapping[str, str]) -> Any:
    """Delegate to :func:`make_gateway_model` which builds a gateway-wired
    ``OpenAIChatModel`` subclass.

    ``LLM_GATEWAY_URL`` and ``LLM_MODEL`` must be set; ``LLM_VIRTUAL_KEY``
    is recommended but falls back to the bearer token when absent.
    """
    return _model.make_gateway_model(task=task, env=env)


def _build_gateway_formatter(task: dict, env: Mapping[str, str]) -> Any:
    """Return an ``OpenAIChatFormatter``.

    The gateway translates to the underlying provider's wire format
    internally, so the agent always uses the OpenAI formatter regardless
    of ``LLM_PROVIDER``.
    """
    del task, env
    from agentscope.formatter import OpenAIChatFormatter

    return OpenAIChatFormatter()


__all__ = ["install"]
