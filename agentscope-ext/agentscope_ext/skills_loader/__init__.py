"""Materialise packaged Skills into the agent's ``Toolkit``.

Wraps ``hooks.build_toolkit`` *on top of* whatever previous bindings
have set it to (typically the MCP injector).  When chained with
:mod:`agentscope_ext.mcp_tenant_injector`, install this *after* it so
Skills layered tools are registered last.

Activation: call :func:`install` exactly once; idempotent.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from agentscope_ext.runtime_worker import hooks as _hooks

from . import loader

_LOG = logging.getLogger(__name__)

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    previous = _hooks.build_toolkit

    def _build_with_skills(
        task: dict, tenant_id: str, env: Mapping[str, str]
    ) -> Any:
        toolkit = previous(task, tenant_id, env)
        skill_names = _coerce_names(task.get("skills"))
        if not skill_names:
            return toolkit
        try:
            # Prefer the dedicated SKILL_STORE_URL; fall back to FOUNDATION_API_URL
            # so the extension keeps working in legacy single-URL deployments.
            base_url = env.get("SKILL_STORE_URL") or env.get("FOUNDATION_API_URL", "")
            # FOUNDATION_SERVICE_SCOPES is injected by the scheduler into the
            # sandbox env.  In prod the API gateway sets X-Service-Scopes after
            # verifying the Bearer JWT; in dev (no gateway) this env var carries
            # the same value so the internal endpoint can authorise the call.
            service_scopes = env.get("FOUNDATION_SERVICE_SCOPES", "")
            manifests = loader.fetch_skill_manifests(
                base_url=base_url,
                token=env.get("FOUNDATION_API_TOKEN", ""),
                tenant_id=tenant_id,
                names=skill_names,
                service_scopes=service_scopes,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("skills_loader: manifest fetch failed: %r", exc)
            return toolkit

        for manifest in manifests:
            try:
                loader.install_skill(toolkit, manifest)
            except Exception as exc:  # noqa: BLE001 -- skip the bad one
                _LOG.warning(
                    "skills_loader: install failed for %r: %r",
                    manifest.get("name"),
                    exc,
                )
        return toolkit

    _hooks.build_toolkit = _build_with_skills


def _coerce_names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(v) for v in value if v]
    return []


__all__ = ["install"]
