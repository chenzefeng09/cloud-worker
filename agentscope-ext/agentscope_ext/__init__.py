"""``agentscope_ext`` namespace package.

Importing this package is side-effect free *unless* the
``RUNTIME_EXTENSIONS`` environment variable is set.  In that case the
listed extensions are auto-installed via :func:`bootstrap.enable_from_env`,
which is what ``python -m agentscope_ext.runtime_worker`` relies on to
activate sibling packages without modifying the runtime entrypoint.

Each extension's ``install()`` is idempotent — repeated calls preserve
the layering established on the first call and do not double-wrap
hooks.
"""

from __future__ import annotations

import os

__all__ = ["enable_all", "enable_from_env"]


def enable_all() -> None:
    """Install all four extensions in the canonical order.

    Order matters for ``build_toolkit``: the MCP injector wraps the
    default first, the Skills loader layers on top so that packaged
    skills can reference tools registered by the MCP servers.
    """
    from .event_audit_sink import install as install_audit
    from .llm_gateway_enforcer import install as install_gateway
    from .mcp_tenant_injector import install as install_mcp
    from .skills_loader import install as install_skills

    install_audit()
    install_gateway()
    install_mcp()
    install_skills()


def enable_from_env() -> None:
    from .bootstrap import enable_from_env as _impl

    _impl()


if os.environ.get("RUNTIME_EXTENSIONS"):
    # Auto-activation path used by the sandbox entrypoint.  Failures
    # here must not break ``import agentscope_ext`` itself — the
    # bootstrap module logs and continues so the runtime can still
    # boot with the default hooks.
    try:
        enable_from_env()
    except Exception:  # noqa: BLE001 -- last-resort safety net
        import logging

        logging.getLogger(__name__).exception(
            "agentscope_ext: auto-bootstrap from RUNTIME_EXTENSIONS failed",
        )
