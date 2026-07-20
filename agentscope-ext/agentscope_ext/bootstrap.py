"""Env-driven extension activation.

The runtime worker entrypoint (``python -m agentscope_ext.runtime_worker``)
deliberately does not import the sibling extensions; instead, importing
the top-level ``agentscope_ext`` package triggers
:func:`enable_from_env` when ``RUNTIME_EXTENSIONS`` is set.

``RUNTIME_EXTENSIONS`` is a comma-separated list of subpackage names,
processed in declaration order.  Unknown names are logged and skipped
so that a typo in one slot does not disable the others.

Each ``install()`` is required to be idempotent: ``enable_from_env``
may be called more than once (e.g. by tests) without re-wrapping any
hook.
"""

from __future__ import annotations

import importlib
import logging
import os
from typing import Iterable

_LOG = logging.getLogger(__name__)

# Map env-name -> dotted module path with an ``install`` callable.
_KNOWN: dict[str, str] = {
    "canvas_tool": "agentscope_ext.canvas_tool",
    "event_audit_sink": "agentscope_ext.event_audit_sink",
    "knowledge_tool": "agentscope_ext.knowledge_tool",
    "llm_gateway_enforcer": "agentscope_ext.llm_gateway_enforcer",
    "mcp_tenant_injector": "agentscope_ext.mcp_tenant_injector",
    "skills_loader": "agentscope_ext.skills_loader",
    "web_fetch": "agentscope_ext.web_fetch",
}


def _split(value: str) -> Iterable[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def enable_from_env(value: str | None = None) -> list[str]:
    """Install extensions named in ``RUNTIME_EXTENSIONS``.

    Returns the list of names that were successfully installed (in the
    order they were applied).  Failures are logged and skipped — they
    must not break callers that may have other extensions to install.
    """
    raw = value if value is not None else os.environ.get("RUNTIME_EXTENSIONS", "")
    installed: list[str] = []
    for name in _split(raw):
        module_path = _KNOWN.get(name)
        if not module_path:
            _LOG.warning("agentscope_ext: unknown extension %r in RUNTIME_EXTENSIONS", name)
            continue
        try:
            module = importlib.import_module(module_path)
            install_fn = getattr(module, "install", None)
            if not callable(install_fn):
                _LOG.warning(
                    "agentscope_ext: %s has no install() callable; skipping", module_path,
                )
                continue
            install_fn()
            installed.append(name)
        except Exception:  # noqa: BLE001 -- defensive
            _LOG.exception("agentscope_ext: failed to install %s", name)
    return installed
